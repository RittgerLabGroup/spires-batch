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

from .nrt import current,choose_sources,day_context,plan_update,finish
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


def cycle(config,through,directory,*,run=False):
    """Prepare one versioned daily update; optionally execute in this allocation."""
    if not os.environ.get('SLURM_JOB_ID'):
        raise RuntimeError('The full native-grid cycle belongs in a compute allocation')
    config=json.loads(Path(config).read_text());root=config['stream_root'];directory=Path(directory).resolve()
    from .acquisition import acquire
    _,_,ledger=current(root);tiles=sorted(config['tiles'])
    policy=config.get('observation_policy')
    if not policy or policy!=ledger['template']['asset_provenance'].get('observation_policy'):
        raise ValueError('Cycle observation policy must match the historical bootstrap policy')
    if policy.get('cloud_model_sha256')!=hashlib.sha256(Path(config['cloud_model']).read_bytes()).hexdigest():
        raise ValueError('Cloud model differs from the registered observation policy')
    if tiles!=sorted(ledger['template']['tiles']):raise ValueError('Cycle tile domain differs from historical state')
    if through<ledger['available_through']:raise ValueError('Availability may not move backwards')
    target_year=int(ledger['template']['start'][:4])+1
    if through>=f'{target_year}-10-01':raise ValueError('Register and bootstrap the next water-year context before crossing October 1')
    acquired=acquire(root,through,directory/'acquisition',collection=config.get('collection','002'),fetch=True)
    inventory,changed=choose_sources(ledger['inventory'],acquired['candidates'],tiles=tiles,collection=config.get('collection','002'),through=through,history_start=ledger['history_start'])
    candidates=[]
    for day in sorted({key.split('/')[1] for key in changed}):
        candidates.extend(prepare_date(config,inventory,day,directory/'prepared'/day))
    update=plan_update(root,candidates,through,directory/'revision',collection=config.get('collection','002'))
    if update.get('status')=='unchanged' or not run:return update
    p=load_plan(update['plan'])
    for stage in p['stage_order']:
        for i in range(len(p['tasks'][stage])):execute(update['plan'],stage,i)
    return finish(directory/'revision/update.json')
