"""Selected NOAA-20 retrieval -> prepared interpolation input, for either stream.

Use maintained matched-observation I/O, inversion and canopy APIs. R0 is an
explicit previously built asset; this worker never constructs or changes it.
"""
import json
from pathlib import Path
import resource
import time
import numpy as np

from .interpolation import file_record,digest,mapped_fields,implementations
from .nrt import source_record,source_identity
from .inputs import cloud_halos


def fit_selected(observed,background,solar,valid,lut):
    """Selected algorithm-6 fit and retained original SSE, in the frozen order."""
    from spires_inversion.invert import _invert_rows
    from spires_inversion import core
    result=np.full((len(observed),6),np.nan,'f8')
    ids=np.flatnonzero(valid&np.isfinite(background).all(axis=1))
    if not len(ids):return result
    z=_invert_rows(observed[ids].astype('f4'),background[ids].astype('f4'),solar[ids].astype('f4'),lut_arrays=lut,
                   max_eval=200,algorithm=6,initial_grain_radius_um=250.,n_workers=1)
    result[ids,:4]=z;result[ids,3]=z[:,3]**2
    for j,i in enumerate(ids):
        snow=np.asarray(core.interpolate_all(lut['reflectances'],lut['bands'],lut['solar_angles'],lut['lap_concentrations'],lut['sqrt_grain_radii'],float(solar[i]),float(z[j,2]),float(z[j,3])),'f8')
        bg=background[i].astype('f8');obs=observed[i].astype('f8')
        model=z[j,0]*snow+(1-z[j,0]-z[j,1])*bg
        result[i,4]=np.sum((obs-model)**2)
        norm=bg@bg;gain=np.clip((obs@bg)/norm,0,1) if norm>0 else 0.
        result[i,5]=np.sum((obs-gain*bg)**2)
    return result


def corrected_magnitude(result,ancillary,geometry):
    import xarray as xr
    from spires_postprocess import apply_snow_fraction_adjustments
    def da(a):return xr.DataArray(np.asarray(a,'f4')[None,:],dims=('y','x'),coords={'y':[0],'x':np.arange(len(a))})
    values=xr.Dataset(dict(fsnow=da(result[:,0]),fshade=da(result[:,1])))
    return apply_snow_fraction_adjustments(values,canopy_fraction=da(ancillary['canopy_fraction']),
        sensor_zenith=da(geometry['sensor_zenith']),sensor_azimuth=da(geometry['sensor_azimuth']),
        slope=da(ancillary['slope']),aspect=da(ancillary['aspect']),canopy_correction_policy='viirs_s2_083').canopy_adjusted_fsnow.values[0]


def screening_flags(raw,ground,valid,cloud,canopy,ice,zenith):
    def finite(a,lo=0,hi=1):return np.isfinite(a)&(a>=lo)&(a<=hi)
    flags=np.zeros(raw.shape,'u1')
    for i,check in enumerate((cloud==0,valid,finite(raw),finite(canopy),np.isfinite(ice)&(ice==0),finite(ground),finite(zenith,0,65))):
        flags[~check]|=np.uint8(1<<i)
    return flags


def retrieve(request,output):
    import h5py
    from spires_io.viirs import open_viirs_surface_reflectance,prepare_viirs_scene_for_inversion
    from spires_inversion.lut import load_reflectance_lut,kernel_lut_arrays
    from spires_inversion import core
    from spires_interpolation.historical import read_record,output_file,DAILY_FIELDS
    from spires_interpolation.observations import prepare_day,positive_swir_score
    r=json.loads(Path(request).read_text()) if isinstance(request,(str,Path)) else request
    actual=source_record(r['source']['path'])
    if any(actual[k]!=r['source'].get(k) for k in actual):raise ValueError('Source changed before retrieval')
    if r['background_available_through']>=actual['date']:raise ValueError('R0 must precede the observation')
    bands=['I1','I2','I3','M2','M4','M8','M11']
    options=dict(r['preparation'])
    if options.get('cloud_mask_application_stage')!='post_inversion':raise ValueError('Retain cloud-independent inversion results for replacement replay')
    options.update(bands=bands,keep_r0_masks=True)
    options['cloud_mask_source']=r['clouds'][actual['tile']]['cloud']
    paths={v['path'] for v in mapped_fields(r)}|{r['lut'],r['source']['path']}
    paths.update(v for d in r['clouds'].values() for v in d.values())
    paths.update(str(v) for k,v in options.items() if k.endswith('_source') and v is not None)
    identity=digest(dict(request=r,inputs={p:file_record(p) for p in sorted(paths)},implementations=implementations()))
    start=time.monotonic()
    # Matched M bands, geometry and QA all follow the same first-I pointer.
    native=scene=None
    try:
        native=open_viirs_surface_reflectance(actual['path'],bands=bands)
        scene=prepare_viirs_scene_for_inversion(native,**options)
        shape=scene.valid_inversion_mask.shape;x,y=scene.x.values,scene.y.values
        axes=read_record(r['axes'],(slice(0,shape[0]),slice(0,shape[1])))
        np.testing.assert_allclose(axes['x'][0],x,rtol=0,atol=1e-6)
        np.testing.assert_allclose(axes['y'][:,0],y,rtol=0,atol=1e-6)
        cloud,context=cloud_halos(actual['tile'],r['clouds'],shape,x=x,y=y)
        lut=kernel_lut_arrays(load_reflectance_lut(r['lut']).sel(band=bands))
        with output_file(output,identity) as h:
            if h is not None:
                for row in range(0,shape[0],20):
                    stop=min(row+20,shape[0]);window=(slice(row,stop),slice(0,shape[1]));grid=(stop-row,shape[1])
                    anc=read_record(r['ancillary'],window);geo={k:scene[k].values[window].ravel() for k in ('solar_zenith','solar_azimuth','sensor_zenith','sensor_azimuth')}
                    bg=read_record(r['background'],window);background=bg['baseline'].copy();used=np.zeros(grid,bool)
                    if 'screened' in bg:
                        used=(bg['index']>=0)&np.isfinite(bg['screened']).all(axis=-1);background[used]=bg['screened'][used]
                    obs=scene.reflectance.values[window].reshape(-1,len(bands));background=background.reshape(obs.shape)
                    valid=scene.valid_inversion_mask.values[window].ravel()
                    result=fit_selected(obs,background,geo['solar_zenith'],valid,lut)
                    ground=corrected_magnitude(result,{k:v.ravel() for k,v in anc.items()},geo).reshape(grid)
                    raw=result.astype('f4').reshape(*grid,6)
                    flags=screening_flags(raw[...,0],ground,valid.reshape(grid),cloud[row+2:stop+2,2:-2],anc['canopy_fraction'],anc['ice_fraction'],geo['sensor_zenith'].reshape(grid))
                    eligible=(flags==0)&(raw[...,0]>np.float32(.04))
                    score=np.full(grid,np.nan,'f8')
                    for i in np.flatnonzero(eligible):
                        p=raw.reshape(-1,6)[i]
                        snow=np.asarray(core.interpolate_all(lut['reflectances'],lut['bands'],lut['solar_angles'],lut['lap_concentrations'],lut['sqrt_grain_radii'],float(geo['solar_zenith'][i]),float(p[2]),float(np.sqrt(p[3].astype('f8')))))
                        fitted=float(p[0])*snow+(1-float(p[0])-float(p[1]))*background[i].astype('f8')
                        score.ravel()[i]=positive_swir_score(obs[i],fitted,bands)
                    d=prepare_day(raw[...,0],ground,flags,cloud[row+2:stop+2,2:-2],native.matched_qa_qf1.values[window],native.matched_qa_qf7.values[window],score,
                                  cloud_halo=cloud[row:stop+4],context_halo=context[row:stop+4])
                    d.update(grain_radius=raw[...,3],dust=raw[...,2],zenith=geo['sensor_zenith'].reshape(grid),solar_zenith=geo['solar_zenith'].reshape(grid),solar_azimuth=geo['solar_azimuth'].reshape(grid))
                    d.update({'retrieval/parameters':raw,'retrieval/canopy_adjusted_fsnow':ground,'retrieval/screen_used':used,'retrieval/swir_score':score})
                    for k,value in d.items():
                        if k not in h:h.create_dataset(k,(*shape,*value.shape[2:]),dtype=value.dtype,chunks=(min(20,shape[0]),min(240,shape[1]),*value.shape[2:]),compression='gzip',compression_opts=2,shuffle=True)
                        h[k][window]=value
                h.create_dataset('x',data=x);h.create_dataset('y',data=y)
                h.attrs.update(source=json.dumps(actual),prepared_context=r['prepared_context'],date=actual['date'],tile=actual['tile'],
                               elapsed_seconds=time.monotonic()-start,max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,canopy_policy='viirs_s2_083')
    finally:
        if scene is not None:scene.close()
        if native is not None:native.close()
    return dict(r['source'],**{k:v for k,v in actual.items() if k not in r['source']},prepared={k:dict(path=str(Path(output).resolve()),dataset=k) for k in DAILY_FIELDS},prepared_context=r['prepared_context'])
