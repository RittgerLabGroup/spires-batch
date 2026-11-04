"""Compute-allocation coordinator for acquire -> cloud -> retrieval -> revision.

Configuration names deployed interpreters/model and explicit tile/WY retrieval
assets. No scheduler submission or live cron modification occurs implicitly.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import hashlib

from .nrt import current,choose_sources,day_context,plan_update,finish,extend_annual_context
from .interpolation import digest,load_plan,execute,implementations


def invoke(python,code,*args):
    subprocess.run([python,'-c',code,*map(str,args)],check=True)


def prepare_date(config,inventory,day,directory):
    """Rebuild the entire same-day domain so cloud halos and donors agree."""
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    tiles=sorted(config['tiles']);context=day_context(inventory,tiles,day)
    year=int(day[:4])+(int(day[5:7])>=10)
    for tile in tiles:
        if inventory.get(tile+'/'+day) is not None and str(year) not in config['tiles'][tile]:
            raise ValueError(f'Missing explicit retrieval assets for {tile}/WY{year}; build/register its annual R0 and context first')
    output=directory/digest(dict(context=context,config=config,implementations=implementations()))
    output.mkdir(parents=True,exist_ok=True)
    inputs={};receipts=[]
    for tile in tiles:
        source=inventory.get(tile+'/'+day)
        if source is None:continue
        matched=output/(tile+'_matched.h5')
        if not matched.exists():
            invoke(config['science_python'],'import sys; from viirs_cloudmask_batch.matched import prepare_matched; prepare_matched(sys.argv[1],sys.argv[2])',source['path'],matched)
        inputs[tile]=str(matched)
    if not inputs:return []
    (output/'cloud-inputs.json').write_text(json.dumps(inputs,indent=2)+'\n')
    invoke(config['cloud_python'],
        'import sys,json,torch,os; from viirs_cloudmask_batch.matched import predict_matched_date; torch.set_num_threads(int(os.environ.get("SLURM_CPUS_PER_TASK","1"))); predict_matched_date(json.load(open(sys.argv[1])),sys.argv[2],sys.argv[3])',
        output/'cloud-inputs.json',output/'cloud',config['cloud_model'])
    clouds={t:dict(cloud=str(output/'cloud'/f'{t}_cloud_mask.tif'),context=str(output/'cloud'/f'{t}_cloud_context_incomplete.tif')) for t in inputs}
    for tile in inputs:
        try:r=copy.deepcopy(config['tiles'][tile][str(year)])
        except KeyError:raise ValueError(f'Missing explicit retrieval assets for {tile}/WY{year}; build/register its annual R0 and context first') from None
        r.update(source=inventory[tile+'/'+day],prepared_context=context,clouds=clouds)
        request=output/(tile+'_retrieval.json');request.write_text(json.dumps(r,indent=2)+'\n')
        receipt=output/(tile+'_prepared.json')
        invoke(config['science_python'],
            'import sys,json; from pathlib import Path; from spires_batch.retrieval import retrieve; r=retrieve(sys.argv[1],sys.argv[2]); Path(sys.argv[3]).write_text(json.dumps(r,indent=2)+"\\n")',
            request,output/(tile+'_prepared.h5'),receipt)
        receipts.append(json.loads(receipt.read_text()))
    return receipts


def _cycle(config,through,directory,*,run=False):
    """Prepare one versioned daily update; optionally execute in this allocation."""
    if not os.environ.get('SLURM_JOB_ID'):
        raise RuntimeError('The full native-grid cycle belongs in a compute allocation')
    config_path=Path(config).resolve();config=json.loads(config_path.read_text())
    root=config['stream_root'];directory=Path(directory).resolve()
    annual=config.get('annual_bright_soil',{})
    for years in annual.values():
        for field in years.values():
            path=Path(field['path']);field['path']=str((config_path.parent/path).resolve() if not path.is_absolute() else path)
    from .acquisition import acquire
    _,_,ledger=current(root);tiles=sorted(config['tiles'])
    policy=config.get('observation_policy')
    if not policy or policy!=ledger['template']['asset_provenance'].get('observation_policy'):
        raise ValueError('Cycle observation policy must match the historical bootstrap policy')
    if policy.get('cloud_model_sha256')!=hashlib.sha256(Path(config['cloud_model']).read_bytes()).hexdigest():
        raise ValueError('Cloud model differs from the registered observation policy')
    if tiles!=sorted(ledger['template']['tiles']):raise ValueError('Cycle tile domain differs from historical state')
    if through<ledger['available_through']:raise ValueError('Availability may not move backwards')
    # Water years select annual inputs, not separate interpolation streams.
    # Preserve the old mappings so a September replacement still uses its R0.
    template=extend_annual_context(ledger['template'],annual)
    from datetime import date,timedelta
    day=date.fromisoformat(ledger['available_through'])+timedelta(days=1)
    end=date.fromisoformat(through);years=set()
    while day<=end:
        years.add(str(day.year+(day.month>=10)));day+=timedelta(days=1)
    for tile in tiles:
        for year in sorted(years):
            if year not in template['tiles'][tile]['bright_soil']:
                raise ValueError(f'Missing annual bright-soil mask: {tile}/WY{year}; register annual_bright_soil without rebuilding history')
            if year not in config['tiles'][tile]:
                raise ValueError(f'Missing explicit retrieval assets for {tile}/WY{year}; retain prior-year recipes for revisions')
    acquired=acquire(root,through,directory/'acquisition',collection=config.get('collection','002'),fetch=True)
    from .nrt_storage import expand
    ledger=expand(ledger,days={rec['date'] for rec in acquired['candidates']})
    inventory,changed=choose_sources(ledger['inventory'],acquired['candidates'],tiles=tiles,collection=config.get('collection','002'),through=through,history_start=ledger['history_start'],calendar_start=(date.fromisoformat(ledger['available_through'])+timedelta(days=1)).isoformat())
    candidates=[]
    for day in sorted({key.split('/')[1] for key in changed}):
        candidates.extend(prepare_date(config,inventory,day,directory/'prepared'/day))
    update=plan_update(root,candidates,through,directory/'revision',collection=config.get('collection','002'),
                       annual_bright_soil=annual,seed_plan=config.get('seed_plan'))
    if update.get('status')=='unchanged' or not run:return update
    p=load_plan(update['plan'])
    for stage in p['stage_order']:
        for i in range(len(p['tasks'][stage])):execute(update['plan'],stage,i)
    return finish(directory/'revision/update.json')


def cycle(config,through,directory,*,run=False):
    if not os.environ.get('SLURM_JOB_ID'):
        raise RuntimeError('The full native-grid cycle belongs in a compute allocation')
    from .nrt_storage import storage_lock,register_workspace,workspace_state,archive_workspace_metadata,Store
    config_path=Path(config).resolve();root=json.loads(config_path.read_text())['stream_root']
    with storage_lock(root):
        register_workspace(root,directory,'running')
        try:
            # Retain the exact dated retrieval recipes and model configuration.
            from .nrt_storage import archive_configuration
            archive_configuration(root,config_path)
            result=_cycle(config_path,through,directory,run=run)
            if run or result.get('status')=='unchanged':
                archive_workspace_metadata(root,directory);workspace_state(root,directory,'complete')
            else:workspace_state(root,directory,'planned')
            return result
        except Exception:
            workspace_state(root,directory,'failed');raise
