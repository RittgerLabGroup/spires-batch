"""Historical interpolation control plane over explicit prepared observations.

Plans freeze source identities, scientific assets and implementations. Commands
never infer a research checkout and never submit jobs as an import side effect.
"""
import argparse
from contextlib import ExitStack
from pathlib import Path
import hashlib
import json
import math
import os
import shlex
import re
import sys


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def file_record(path,checksum=False):
    p=Path(path).resolve();s=p.stat()
    value=dict(path=str(p),bytes=s.st_size,mtime_ns=s.st_mtime_ns)
    if checksum:value['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    return value


def implementations():
    import spires_interpolation
    root=Path(spires_interpolation.__file__).parent
    files=list(root.glob('*.py'))+list((root/'native').glob('*.cpp'))+list(Path(__file__).parent.glob('*.py'))
    return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def scientific_implementations(code):
    """State compatibility follows numerical workers, not controller revisions."""
    result={}
    for path,value in code.items():
        p=Path(path)
        if p.parent.name=='spires_interpolation':result['spires_interpolation/'+p.name]=value
        elif p.parent.name=='native' and p.parent.parent.name=='spires_interpolation':
            result['spires_interpolation/native/'+p.name]=value
    if not result:raise ValueError('Plan lacks scientific implementation provenance')
    return result


def state_context(request,inputs,code):
    """Separate fixed science from append-only, observation-year ancillary assets.

    R0 belongs to each prepared observation's provenance, not the restart context.
    Adding the next annual mask must not invalidate already screened history.
    """
    context={tile:{k:v for k,v in fields.items() if k not in ('daily','bright_soil')}
             for tile,fields in request['tiles'].items()}
    paths={r['path'] for r in mapped_fields(context)}
    fixed=dict(policy='noaa20-frequency20-cap25-rare01-coastal-v1',
        history_start=request['history_start'],shape=request['shape'],stripe_rows=request.get('stripe_rows',20),
        source_selection='NOAA20-first-I-matched-M-geometry-v1',context=context,
        assets={p:inputs[p] for p in sorted(paths)},
        native={k:inputs[request[k]]['sha256'] for k in ('common_library','spatial_library')},
        albedo_lut=inputs[request['albedo_lut']]['sha256'],implementations=scientific_implementations(code),
        observation_policy=request['asset_provenance'].get('observation_policy',{}))
    annual={tile:{year:dict(field=field,file=inputs[field['path']])
                  for year,field in fields['bright_soil'].items()} for tile,fields in request['tiles'].items()}
    return dict(schema_version=1,fixed=fixed,annual_bright_soil=annual)


def compatible_context(previous,current):
    """Require unchanged science and existing masks; allow new annual entries."""
    if previous['schema_version']!=current['schema_version'] or previous['fixed']!=current['fixed']:
        raise ValueError('Restart scientific implementation or fixed context changed')
    for tile,years in previous['annual_bright_soil'].items():
        for year,record in years.items():
            if current['annual_bright_soil'].get(tile,{}).get(year)!=record:
                raise ValueError(f'Existing annual bright-soil context changed: {tile}/WY{year}')


def mapped_fields(value):
    if isinstance(value,dict):
        if 'path' in value and 'dataset' in value:yield value
        else:
            for child in value.values():yield from mapped_fields(child)
    elif isinstance(value,list):
        for child in value:yield from mapped_fields(child)


def active_fields(request):
    """A checkpoint owns its earlier observations; reopen only replay inputs."""
    for fields in request['tiles'].values():
        yield from mapped_fields({k:v for k,v in fields.items() if k!='daily'})
        first=request['start'] if 'seed' in request else request['history_start']
        for day,record in fields['daily'].items():
            if first<=day<=request['available_through']:yield from mapped_fields(record)


def stage_resources(scheduler,stage):
    return {**scheduler,**scheduler.get('stages',{}).get(stage,{})}


def plan(config,destination,*,header_cache=None):
    import numpy as np
    import h5py
    from spires_interpolation.historical import DAILY_FIELDS
    config=Path(config).resolve();request=json.loads(config.read_text())
    required={'schema_version','history_start','start','stop','available_through','shape','tiles','output','common_library','spatial_library','albedo_lut','scheduler','asset_provenance'}
    optional={'stripe_rows','source_stream','seed','seed_plan','stream_root'}
    if required-set(request) or set(request)-required-optional or request['schema_version']!=1:
        raise ValueError('Invalid interpolation request fields/schema')
    if request.get('source_stream','standard') not in ('standard','nrt','mixed'):
        raise ValueError('Invalid source stream')
    dates=[np.datetime64(request[k],'D') for k in ('history_start','start','stop','available_through')]
    history,start,stop,available=dates
    if any(np.isnat(d) for d in dates) or not history<=start<stop<=available+1:
        raise ValueError('Invalid history/target/availability interval')
    if str(start)[5:]!='10-01' and 'seed' not in request:
        raise ValueError('Historical requests begin October 1; incremental runs use checkpoints')
    if len(request['shape'])!=2 or min(request['shape'])<1 or request.get('stripe_rows',20)<1:
        raise ValueError('Invalid grid or stripe size')
    if not request['tiles']:raise ValueError('No tiles configured')
    # Resolve paths once. Workers never depend on their current directory.
    for rec in mapped_fields(request):
        p=Path(rec['path']).expanduser();rec['path']=str((config.parent/p).resolve() if not p.is_absolute() else p.resolve())
    for key in ('output','common_library','spatial_library','albedo_lut'):
        p=Path(request[key]).expanduser();request[key]=str((config.parent/p).resolve() if not p.is_absolute() else p.resolve())
    scheduler=request['scheduler']
    if set(scheduler)-{'stages'}!={'partition','account','policy','cpus','memory','time','concurrency'}:
        raise ValueError('Scheduler requires partition/account/policy/cpus/memory/time/concurrency')
    policy=Path(scheduler['policy']);policy=(config.parent/policy).resolve() if not policy.is_absolute() else policy
    scheduler['policy']=str(policy);scheduler['snapshot']=json.loads(policy.read_text())
    if not isinstance(scheduler['snapshot'].get('exclude_nodes'),list):raise ValueError('Scheduler policy lacks exclude_nodes')
    for key in ('partition','account'):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',scheduler[key]):raise ValueError('Invalid scheduler name')
    if set(scheduler.get('stages',{}))-{'screen','spatial','properties','export'}:raise ValueError('Unknown scheduler stage')
    for override in scheduler.get('stages',{}).values():
        if set(override)-{'cpus','memory','time','concurrency'}:raise ValueError('Invalid stage resource override')
    for stage in ('screen','spatial','properties','export'):
        resources=stage_resources(scheduler,stage)
        if not re.fullmatch(r'[0-9]+[KMGT]?',resources['memory']) or not re.fullmatch(r'[0-9:-]+',resources['time']):raise ValueError('Invalid memory or walltime')
        if any(not isinstance(resources[k],int) or resources[k]<1 for k in ('cpus','concurrency')):raise ValueError('Invalid worker allocation')
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+',node) for node in scheduler['snapshot']['exclude_nodes']):raise ValueError('Invalid excluded node')
    for tile,t in request['tiles'].items():
        if set(t)!={'daily','terrain','bright_soil','agriculture','coastal','frequency'} or len(t['frequency'])!=12:
            raise ValueError(f'Incomplete tile context: {tile}')
        if set(t['terrain'])!={'x','y','dem','slope','aspect','canopy_fraction'}:raise ValueError('Incomplete terrain')
        for day in np.arange(start if 'seed' in request else history,available+1):
            key=str(day)
            if key not in t['daily']:raise ValueError(f'Missing explicit daily record (use null for missing): {tile}/{key}')
            if t['daily'][key] is not None and set(t['daily'][key])!=set(DAILY_FIELDS):raise ValueError(f'Incomplete prepared fields: {tile}/{key}')
            wy=int(key[:4])+(int(key[5:7])>=10)
            if str(wy) not in t['bright_soil']:raise ValueError(f'Missing annual bright-soil mask: {tile}/{wy}')
    paths={r['path'] for r in active_fields(request)}
    inputs={p:file_record(p) for p in sorted(paths)}
    for field in active_fields(request):
        if 'archive_file' in field and any(inputs[field['path']][k]!=field['archive_file'][k] for k in ('bytes','mtime_ns')):
            raise ValueError('Prepared input differs from its archived identity')
    for p in (request['common_library'],request['spatial_library'],request['albedo_lut'],str(policy)):
        inputs[p]=file_record(p,True)
    if 'seed' in request:
        if set(request['seed'])!=set(request['tiles']):raise ValueError('Restart requires every donor-domain tile')
        for tile,path in request['seed'].items():
            path=Path(path);path=(config.parent/path).resolve() if not path.is_absolute() else path.resolve()
            request['seed'][tile]=str(path);inputs[str(path)]=file_record(path)
    # Annual additions extend the chronological stream. Neither the output year
    # nor the R0 of newer observations changes already computed history.
    code=implementations()
    request['state_context']=state_context(request,inputs,code)
    request['state_identity']=digest(request['state_context']['fixed'])
    # Open each source once for all dataset header checks; no pixel arrays loaded.
    grouped={}
    for rec in active_fields(request):grouped.setdefault(rec['path'],[]).append(rec)
    headers={} if header_cache is None else header_cache
    for path,records in grouped.items():
        key=(path,inputs[path]['bytes'],inputs[path]['mtime_ns'])
        shapes=headers.setdefault(key,{})
        names={rec['dataset'] for rec in records}-shapes.keys()
        if names:
            with h5py.File(path,'r') as h:shapes.update({name:h[name].shape for name in names})
        for rec in records:
            full=shapes[rec['dataset']]
            expected=(request['shape'][1 if rec['axis']=='x' else 0],) if 'axis' in rec else tuple(request['shape'])
            shape=full[1:] if 'index' in rec else full
            if 'band' in rec:
                if not isinstance(rec['band'],int) or not 0<=rec['band']<shape[-1]:raise ValueError('Invalid trailing band')
                shape=shape[:-1]
            if shape!=expected:raise ValueError(f'Grid mismatch: {path}:{rec["dataset"]}')
            if 'index' in rec and not 0<=rec['index']<full[0]:raise ValueError('Context index outside source')
    n=math.ceil(request['shape'][0]/request.get('stripe_rows',20))
    if 'seed' in request:
        inherited=set();legacy=None
        for path in request['seed'].values():
            with h5py.File(path) as h:
                if not h.attrs.get('complete') or not h.attrs.get('state_identity') or len(h)!=n:
                    raise ValueError('Incomplete restart or stripe layout mismatch')
                inherited.add(h.attrs['state_identity'])
                if 'state_context' in h.attrs:
                    previous=json.loads(h.attrs['state_context'])
                else:
                    # Older bundles contain the same numerical state but lack
                    # the separate context record. Verify their original plan;
                    # do not regenerate or relabel their checkpoint arrays.
                    if 'seed_plan' not in request:raise ValueError('Legacy checkpoint requires its original seed_plan for provenance validation')
                    if legacy is None:
                        source=Path(request['seed_plan']);source=(config.parent/source).resolve() if not source.is_absolute() else source
                        request['seed_plan']=str(source);inputs[str(source)]=file_record(source,True)
                        legacy=load_plan(source,compatible_state=True)
                    if h.attrs.get('identity')!=legacy['identity'] or h.attrs['state_identity']!=legacy['request']['state_identity']:
                        raise ValueError('Legacy checkpoint does not belong to seed_plan')
                    previous=state_context(legacy['request'],legacy['inputs'],legacy['implementations'])
                compatible_context(previous,request['state_context'])
                for group in h.values():
                    if 'age_history' not in group:raise ValueError('Checkpoint predates rewindable age state; rebuild historical state')
        if len(inherited)!=1:raise ValueError('Restart tiles have different state identities')
        request['state_identity']=inherited.pop()
    tasks={'screen':[dict(tile=t,stripe=s) for t in request['tiles'] for s in range(n)],
           'spatial':[dict(day=str(d)) for d in np.arange(start,stop)],
           'properties':[dict(tile=t,stripe=s) for t in request['tiles'] for s in range(n)],
           'export':[dict(tile=t) for t in request['tiles']]}
    record=dict(artifact_type='spires_interpolation_plan',schema_version=1,request=request,inputs=inputs,
                implementations=code,tasks=tasks,stage_order=['screen','spatial','properties','export'])
    record['identity']=digest(record)
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():
        if json.loads(destination.read_text())!=record:raise FileExistsError('Existing plan differs')
    else:
        with destination.open('x') as f:json.dump(record,f,indent=2);f.write('\n')
    return record


def load_plan(path,*,compatible_state=False):
    p=json.loads(Path(path).read_text());identity=p.pop('identity')
    if digest(p)!=identity:raise ValueError('Plan digest mismatch')
    p['identity']=identity
    code=implementations()
    if compatible_state:
        if scientific_implementations(p['implementations'])!=scientific_implementations(code):
            raise ValueError('Historical scientific implementation differs from deployed workers')
    elif p['implementations']!=code:raise ValueError('Implementation changed since planning')
    required=None
    if compatible_state:
        r=p['request'];required={v['path'] for fields in r['tiles'].values() for k,value in fields.items() if k!='daily' for v in mapped_fields(value)}
        required.update(r[k] for k in ('common_library','spatial_library','albedo_lut'))
    for rec in p['inputs'].values():
        if required is not None and rec['path'] not in required:continue
        got=file_record(rec['path'],False)
        if any(got[k]!=rec[k] for k in ('bytes','mtime_ns')):raise ValueError(f'Input changed: {rec["path"]}')
    return p


def execute(path,stage,index):
    from contextlib import nullcontext
    from .nrt_storage import storage_lock
    raw=json.loads(Path(path).read_text());root=raw['request'].get('stream_root')
    with storage_lock(root) if root else nullcontext():return _execute(path,stage,index)


def _execute(path,stage,index):
    p=load_plan(path);r=p['request'];task=p['tasks'][stage][index]
    from spires_interpolation.historical import screen_stripe,spatial_day,complete_stripe
    if stage=='screen':return screen_stripe(r,identity=p['identity'],**task)
    if stage=='spatial':return spatial_day(r,identity=p['identity'],threads=stage_resources(r['scheduler'],stage)['cpus'],**task)
    if stage=='properties':return complete_stripe(r,identity=p['identity'],**task)
    if stage=='export':return export_tile(p,task['tile'])
    raise ValueError('Unknown stage')


def export_tile(plan,tile):
    import h5py,numpy as np,netCDF4
    from spires_interpolation.historical import property_path,screen_path,ready,terrain
    from spires_interpolation.netcdf_export import copy_stripes
    from spires_interpolation.publication import publish_generation
    from spires_interpolation.albedo import FLAG_MASKS,FLAG_MEANINGS
    r=plan['request'];identity=plan['identity'];height,width=r['shape'];rows=r.get('stripe_rows',20)
    stripes=[(row,property_path(r,tile,i)) for i,row in enumerate(range(0,height,rows))]
    for _,path in stripes:
        if not ready(path,identity):raise FileNotFoundError(path)
    dates=np.arange(np.datetime64(r['start']),np.datetime64(r['stop']))
    final_count=int(np.count_nonzero(dates<=np.datetime64(r['available_through'])-7))
    with h5py.File(stripes[0][1],'r') as h:
        variables={k:h[k].dtype for k in h if isinstance(h[k],h5py.Dataset) and h[k].ndim in (3,4)}
    a=terrain(r,tile,(slice(0,height),slice(0,width)))
    def build(folder):
        artifacts={};outputs={}
        with ExitStack() as stack:
            for issue,count in ((0,len(dates)),(1,final_count)):
                if not count:continue
                path=folder/('first.nc' if issue==0 else 'final.nc');out=stack.enter_context(netCDF4.Dataset(path,'w'));outputs[issue]=out
                for name,size in [('time',count),('y',height),('x',width)]:out.createDimension(name,size)
                t=out.createVariable('time','i8',('time',));t[:]=dates[:count].astype('i8');t.units='days since 1970-01-01';t.calendar='proleptic_gregorian'
                for name,values in [('x',a['x'][0]),('y',a['y'][:,0])]:
                    v=out.createVariable(name,'f8',(name,));v[:]=values;v.units='m';v.standard_name='projection_'+name+'_coordinate'
                crs=out.createVariable('crs','i4');crs.grid_mapping_name='sinusoidal';crs.longitude_of_central_meridian=0.;crs.earth_radius=6371007.181;crs.false_easting=0.;crs.false_northing=0.
                for name,dtype in variables.items():
                    v=out.createVariable(name,dtype.newbyteorder('='),('time','y','x'),zlib=True,complevel=2,shuffle=True,chunksizes=(min(8,count),min(20,height),min(240,width)))
                    v.grid_mapping='crs';v.long_name=name.replace('_',' ')
                    v.units='um' if name=='grain_radius' else 'ppm' if name=='dust' else 'days' if name.startswith('days_since') else '1'
                    if name in ('albedo_flags','albedo_terrain_flags'):v.flag_masks=FLAG_MASKS;v.flag_meanings=FLAG_MEANINGS
                    if name in ('grain_radius_source','dust_source'):v.flag_values=np.array([0,1,2,3,6],'u1');v.flag_meanings='not_snow direct temporal spatial constant_fallback'
                out.setncatts(dict(Conventions='CF-1.8',tile=tile,policy='noaa20-frequency20-cap25-rare01-coastal-v1',issue='first' if issue==0 else 'final',future_days=issue*7,source_stream=r.get('source_stream','standard'),plan_identity=identity,asset_provenance=json.dumps(r['asset_provenance'],sort_keys=True)))
                artifacts['first' if issue==0 else 'final']=path
            copy_stripes(stripes,outputs,variables=variables,max_array_bytes=128*1024**2)
        # Publish restart state in the same generation as its data products.
        with h5py.File(folder/'checkpoints.h5','w') as h:
            for i,(_,path) in enumerate(stripes):
                g=h.create_group(f'{i:03d}')
                with h5py.File(screen_path(r,tile,i),'r') as source:
                    source.copy('checkpoint',g);source.copy('last_observed_fsca',g)
                    source.copy('age_history',g)
                    g.attrs['fsca_processed_day']=source.attrs['processed_day']
                with h5py.File(path,'r') as source:
                    source.copy('observation_age_checkpoint',g)
                    source.copy('property_history_checkpoint',g)
            h.attrs.update(complete=True,identity=identity,state_identity=r['state_identity'],
                           state_context=json.dumps(r['state_context'],sort_keys=True))
        from .nrt_storage import checkpoint_coverage
        coverage=checkpoint_coverage(folder/'checkpoints.h5')
        with h5py.File(folder/'checkpoints.h5','r+') as h:
            h.attrs['restart_coverage']=json.dumps(coverage)
        artifacts['checkpoints']=folder/'checkpoints.h5'
        return artifacts
    def validate(paths):
        for name,path in paths.items():
            if name=='checkpoints':
                with h5py.File(path) as h:
                    if len(h)!=len(stripes) or not h.attrs['complete']:raise ValueError('Incomplete checkpoints')
            else:
                with netCDF4.Dataset(path) as ds:
                    if len(ds.dimensions['time'])!=(len(dates) if name=='first' else final_count):raise ValueError('Wrong issue length')
                    for variable in variables:
                        if ds[variable].shape[1:]!=(height,width):raise ValueError('Wrong export grid')
    return publish_generation(Path(r['output'])/'products'/tile,request=dict(plan=identity,tile=tile),expected_parent=None,build=build,validate=validate)


def render(plan_path,directory):
    p=load_plan(plan_path);s=p['request']['scheduler'];directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=True)
    excludes=','.join(s['snapshot']['exclude_nodes']);python=shlex.quote(sys.executable);planarg=shlex.quote(str(Path(plan_path).resolve()))
    import spires_interpolation
    source_roots=os.pathsep.join((str(Path(__file__).resolve().parents[1]),str(Path(spires_interpolation.__file__).resolve().parents[1])))
    scripts=[]
    for stage in p['stage_order']:
        s=stage_resources(p['request']['scheduler'],stage)
        for begin in range(0,len(p['tasks'][stage]),400):
            end=min(begin+400,len(p['tasks'][stage]))-1
            path=directory/f'{stage}-{begin:04d}.sbatch'
            lines=['#!/bin/bash','set -euo pipefail',f'export PYTHONPATH={shlex.quote(source_roots)}${{PYTHONPATH:+:$PYTHONPATH}}',
                   'export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1',
                   f'{python} -m spires_batch.interpolation execute-task {planarg} {stage} "$SLURM_ARRAY_TASK_ID"']
            directives=[f'#SBATCH --partition={s["partition"]}',f'#SBATCH --account={s["account"]}',f'#SBATCH --cpus-per-task={s["cpus"]}',f'#SBATCH --mem={s["memory"]}',f'#SBATCH --time={s["time"]}',f'#SBATCH --array={begin}-{end}%{s["concurrency"]}',f'#SBATCH --output={directory}/%x-%A_%a.out']
            if excludes:directives.append(f'#SBATCH --exclude={excludes}')
            path.write_text('\n'.join([lines[0],*directives,*lines[1:]])+'\n');scripts.append(dict(stage=stage,path=str(path)))
    (directory/'scheduler_policy.json').write_text(json.dumps(s['snapshot'],indent=2)+'\n')
    # Explicit launcher; rendering never submits. Whole stages use afterok edges.
    lines=['#!/bin/bash','set -euo pipefail','parents=""']
    for stage in p['stage_order']:
        lines+=['jobs=()']
        for item in scripts:
            if item['stage']==stage:
                lines += ['dependency=()', 'if [[ -n "$parents" ]]; then dependency=("--dependency=afterok:$parents"); fi',
                          'job=$(sbatch --parsable "${dependency[@]}" '+shlex.quote(item['path'])+')',
                          'job=${job%%;*}', 'jobs+=("$job")', 'parents=$job',
                          'printf "%s\\t%s\\n" '+shlex.quote(stage)+' "$job" >> '+shlex.quote(str(directory/'submissions.tsv'))]
        lines+=['parents=$(IFS=:; echo "${jobs[*]}")']
    (directory/'submit.sh').write_text('\n'.join(lines)+'\n')
    return scripts


def main(argv=None):
    parser=argparse.ArgumentParser(prog='spires-batch interpolation');commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('plan');p.add_argument('config');p.add_argument('--output',required=True)
    p=commands.add_parser('execute-task');p.add_argument('plan');p.add_argument('stage',choices=['screen','spatial','properties','export']);p.add_argument('index',type=int)
    p=commands.add_parser('render-slurm');p.add_argument('plan');p.add_argument('--output',required=True)
    p=commands.add_parser('status');p.add_argument('plan')
    args=parser.parse_args(argv)
    if args.command=='plan':
        p=plan(args.config,args.output);print(json.dumps(dict(identity=p['identity'],tasks={k:len(v) for k,v in p['tasks'].items()})))
    elif args.command=='execute-task':print(execute(args.plan,args.stage,args.index))
    elif args.command=='render-slurm':print(json.dumps(render(args.plan,args.output),indent=2))
    else:
        p=load_plan(args.plan)
        from spires_interpolation.historical import screen_path,spatial_path,property_path,ready
        paths={'screen':lambda t:screen_path(p['request'],**t),'properties':lambda t:property_path(p['request'],**t)}
        result={stage:dict(total=len(p['tasks'][stage]),complete=sum(ready(fn(t),p['identity']) for t in p['tasks'][stage])) for stage,fn in paths.items()}
        result['spatial']=dict(total=len(p['tasks']['spatial']),complete=sum(all(ready(spatial_path(p['request'],tile,t['day']),p['identity']) for tile in p['request']['tiles']) for t in p['tasks']['spatial']))
        result['export']=dict(total=len(p['tasks']['export']),complete=sum((Path(p['request']['output'])/'products'/t['tile']/'current.json').is_file() for t in p['tasks']['export']))
        print(json.dumps(result,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
