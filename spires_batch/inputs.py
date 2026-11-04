"""Prepare normalized interpolation observations from retained production retrievals.

Field bindings make legacy HDF5 and grouped production NetCDF readable without
research imports. Original retrieval parameters, matched QA, corrected cloud
context and verified SWIR diagnostics must refer to the same source vintage.
"""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import resource
import time
import numpy as np
from .interpolation import digest,file_record,mapped_fields,implementations


def reconstruct_swir(d, needed, spectral, window, raw_scene, lut):
    """Rebuild original fitted spectra and verify SSE before accepting new scores."""
    from spires_interpolation.historical import read_record
    from spires_interpolation.observations import positive_swir_score
    from spires_inversion import core
    bands=spectral['bands']
    extra=read_record({k:spectral[k] for k in ('shade','sse','background')},window)
    if 'screened_background' in spectral:
        selection=read_record({k:spectral[k] for k in ('screened_background','screen_used')},window)
        use=selection['screen_used'].astype(bool)
        extra['background'][use]=selection['screened_background'][use]
    rr,cc=np.nonzero(needed);native_rows=rr+window[0].start
    observed=np.stack([(raw_scene.reflectance_500m.sel(band_500m=b).values if b.startswith('I')
                       else raw_scene.matched_reflectance_1km.sel(band_1km=b).values)[native_rows,cc] for b in bands],axis=-1).astype('f8')
    fit=np.empty_like(observed)
    for i,(row,col) in enumerate(zip(rr,cc)):
        snow=np.asarray(core.interpolate_all(lut['reflectances'],lut['bands'],lut['solar_angles'],lut['lap_concentrations'],lut['sqrt_grain_radii'],
                         float(d['solar_zenith'][row,col]),float(d['dust'][row,col]),float(np.sqrt(d['grain_radius'][row,col]))))
        f=float(d['raw'][row,col]);shade=float(extra['shade'][row,col])
        fit[i]=f*snow+(1-f-shade)*extra['background'][row,col].astype('f8')
    sse=np.sum((observed-fit)**2,axis=-1)
    np.testing.assert_allclose(sse,extra['sse'][needed],rtol=2e-5,atol=2e-7,err_msg='Original spectrum/background/LUT provenance mismatch')
    score=positive_swir_score(observed,fit,bands)
    if not np.isfinite(score).all():raise ValueError('Invalid reconstructed SWIR score')
    return score


def cloud_halos(tile,clouds,shape,*,x=None,y=None):
    import rasterio
    height,width=shape
    cloud=np.full((height+4,width+4),255,'u1');context=np.ones(cloud.shape,'u1')
    for other,paths in clouds.items():
        dh=int(other[1:3])-int(tile[1:3]);dv=int(other[4:6])-int(tile[4:6])
        lo_y,hi_y=max(-2,dv*height),min(height+2,(dv+1)*height)
        lo_x,hi_x=max(-2,dh*width),min(width+2,(dh+1)*width)
        if lo_y>=hi_y or lo_x>=hi_x:continue
        selection=((lo_y-dv*height,hi_y-dv*height),(lo_x-dh*width,hi_x-dh*width))
        dest=(slice(lo_y+2,hi_y+2),slice(lo_x+2,hi_x+2))
        for key,target in [('cloud',cloud),('context',context)]:
            with rasterio.open(paths[key]) as ds:
                if ds.shape!=tuple(shape):raise ValueError('Cloud/native grid mismatch')
                if x is not None:
                    px,py=ds.xy(0,0)
                    if not np.isclose(px,x[0]+dh*width*(x[1]-x[0]),rtol=0,atol=1e-5) or not np.isclose(py,y[0]+dv*height*(y[1]-y[0]),rtol=0,atol=1e-5):raise ValueError('Cloud grid origin mismatch')
                    if not np.isclose(ds.transform.a,x[1]-x[0],rtol=0,atol=1e-6) or not np.isclose(ds.transform.e,y[1]-y[0],rtol=0,atol=1e-6):raise ValueError('Cloud grid spacing mismatch')
                values=ds.read(1,window=selection)
            if not np.isin(values,[0,1,255]).all():raise ValueError('Invalid cloud/context codes')
            target[dest]=values if key=='cloud' else (values==1).astype('u1')
    return cloud,context


def prepare_retained(request,output):
    from spires_interpolation.historical import read_record,output_file,DAILY_FIELDS
    from spires_interpolation.observations import prepare_day
    import h5py
    request=json.loads(Path(request).read_text()) if isinstance(request,(str,Path)) else request
    fields=request['fields'];shape=tuple(request['shape']);tile=request['tile']
    required={'raw','direct','screening_flags','qa_qf1','qa_qf7','zenith','grain_radius','dust','solar_zenith','solar_azimuth'}
    if set(fields) not in (required|{'swir_flags'},required|{'swir_score'}) and not (set(fields)==required and 'spectral' in request):
        raise ValueError('Incomplete retained retrieval/SWIR bindings')
    paths={v['path'] for v in mapped_fields(request)}
    paths.update(p for rec in request['clouds'].values() for p in rec.values())
    inputs={p:file_record(p) for p in sorted(paths)}
    if 'spectral' in request:inputs[request['spectral']['lut']]=file_record(request['spectral']['lut'],True)
    from .nrt import source_record,source_identity
    actual=source_record(request['source']['path'])
    if any(actual[k]!=request['source'].get(k) for k in actual):raise ValueError('Retained retrieval has a different source vintage')
    if actual['date']!=request['date'] or actual['tile']!=tile:raise ValueError('Retained request date/tile differs from native source')
    identity=digest(dict(request=request,inputs=inputs,implementations=implementations()))
    axes=read_record(request['axes'],(slice(0,shape[0]),slice(0,shape[1])))
    x,y=axes['x'][0],axes['y'][:,0]
    cloud,context=cloud_halos(tile,request['clouds'],shape,x=x,y=y)
    begin=time.monotonic()
    raw_scene=lut=None
    with ExitStack() as resources, output_file(output,identity) as h:
        if h is not None:
            for row in range(0,shape[0],20):
                stop=min(row+20,shape[0]);window=(slice(row,stop),slice(0,shape[1]))
                d=read_record(fields,window)
                if 'swir_flags' not in d and 'swir_score' not in d:d['swir_flags']=np.zeros(d['raw'].shape,'u1')
                corrected=(d['screening_flags']&254)|(cloud[row+2:stop+2,2:-2]!=0).astype('u1')
                eligible=(corrected==0)&np.isfinite(d['direct'])&np.isfinite(d['raw'])&(d['raw']>.04)&(d['raw']<=1)
                needed=eligible&(((d['swir_flags']&4)==0) if 'swir_flags' in d else ~np.isfinite(d['swir_score']))
                if needed.any() and 'spectral' in request:
                    spec=request['spectral']
                    if raw_scene is None:
                        from spires_io.viirs import open_viirs_surface_reflectance
                        from spires_inversion.lut import load_reflectance_lut,kernel_lut_arrays
                        raw_scene=open_viirs_surface_reflectance(actual['path'],bands=spec['bands'])
                        resources.callback(raw_scene.close)
                        np.testing.assert_allclose(raw_scene.x_500m.values,x,rtol=0,atol=1e-6)
                        np.testing.assert_allclose(raw_scene.y_500m.values,y,rtol=0,atol=1e-6)
                        lut=kernel_lut_arrays(load_reflectance_lut(spec['lut']).sel(band=spec['bands']))
                    score=reconstruct_swir(d,needed,spec,window,raw_scene,lut)
                    if 'swir_flags' in d:d['swir_flags'][needed]=(4|((score>.05).astype('u1')<<1))
                    else:d['swir_score'][needed]=score
                result=prepare_day(d['raw'],d['direct'],d['screening_flags'],cloud[row+2:stop+2,2:-2],d['qa_qf1'],d['qa_qf7'],d.get('swir_score'),
                    cloud_halo=cloud[row:stop+4],context_halo=context[row:stop+4],swir_flags=d.get('swir_flags'))
                result.update({k:d[k] for k in ('zenith','grain_radius','dust','solar_zenith','solar_azimuth')})
                for key in DAILY_FIELDS:
                    value=result[key]
                    if key not in h:h.create_dataset(key,shape,dtype=value.dtype,chunks=(min(20,shape[0]),min(240,shape[1])),compression='gzip',compression_opts=2,shuffle=True)
                    h[key][window]=value
            h.create_dataset('x',data=x);h.create_dataset('y',data=y)
            h.attrs.update(tile=tile,date=request['date'],source=json.dumps(actual),prepared_context=request['prepared_context'],
                           elapsed_seconds=time.monotonic()-begin,max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    record={**request['source'],**actual}
    record.update(prepared={k:dict(path=str(Path(output).resolve()),dataset=k) for k in DAILY_FIELDS},prepared_context=request['prepared_context'])
    return record


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('request');p.add_argument('--output',required=True);p.add_argument('--receipt',required=True)
    a=p.parse_args(argv);result=prepare_retained(a.request,a.output)
    Path(a.receipt).write_text(json.dumps(result,indent=2)+'\n');return 0


if __name__=='__main__':raise SystemExit(main())
