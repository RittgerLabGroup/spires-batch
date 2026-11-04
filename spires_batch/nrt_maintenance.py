"""Explicit migration, rebuild and retention commands for operational NRT."""
import copy
import json
from pathlib import Path

from .interpolation import digest,plan,execute,mapped_fields,file_record
from .nrt_storage import (storage_lock,register_workspace,workspace_state,Store,
                          expand,archive_ledger,atomic_json,settings,checked,durable_ledger)


def migrate(root,*,seed_plan=None):
    """Archive existing catalogs and adopt unchanged state; no numerical replay."""
    from .nrt import current,commit,ledger_context
    from spires_interpolation.publication import artifact_paths
    root=Path(root).resolve()
    with storage_lock(root):
        g,paths,ledger=current(root)
        if ledger.get('schema_version')==2:return dict(status='unchanged',generation=g['generation'])
        if seed_plan:ledger['template']['seed_plan']=Store(root).put(seed_plan,'plans')['path']
        ledger['state_context']=ledger_context(ledger,seed_plan)
        from .nrt_storage import archive_provenance
        original=seed_plan or ledger['template'].get('seed_plan')
        if original:ledger['provenance']=archive_provenance(root,original)
        # Preserve each earlier as-issued inventory and all its products before
        # making any of its working files eligible for later cleanup.
        for manifest in (root/'generations').glob('*/manifest.json'):
            old=json.loads(manifest.read_text());op=artifact_paths(root,old)
            prior=json.loads(op['ledger'].read_text())
            prior['state_context']=ledger_context(prior,seed_plan)
            if seed_plan:prior['template']['seed_plan']=ledger['template']['seed_plan']
            if original:prior.setdefault('provenance',ledger['provenance'])
            converted=archive_ledger(root,prior)
            atomic_json(Path(settings(root)['archive_root'])/'publications'/(old['generation']+'.json'),dict(generation=old,ledger=durable_ledger(root,converted)))
        return commit(root,ledger,{t:paths['checkpoint_'+t] for t in ledger['template']['tiles']},
                      expected_parent=g['generation'],identity=dict(storage_migration=g['generation'],schema_version=2))


def rebuild(root,directory,*,checkpoint=None):
    """Freeze an explicit full-history or selected-recovery replay from the archive."""
    import numpy as np
    from .nrt import current
    from .nrt_storage import recoveries,checkpoint_coverage
    root=Path(root).resolve();directory=Path(directory).resolve()
    with storage_lock(root):
        register_workspace(root,directory)
        try:
            g,_,ledger=current(root);complete=expand(ledger)
            r=copy.deepcopy(complete['template']);r.pop('seed',None)
            r.update(stop=str(np.datetime64(ledger['available_through'])+1),available_through=ledger['available_through'],
                     output=str(directory/'work'),source_stream='mixed',stream_root=str(root))
            if not checkpoint and 'provenance' in ledger:
                provenance=json.loads(checked(ledger['provenance']).read_text())
                assets=provenance.get('assets',{})
                def restore(path):
                    if path not in assets:return path
                    rec=assets[path];old=Path(path)
                    if old.is_file() and all(file_record(old)[k]==rec[k] for k in ('bytes','mtime_ns')):return path
                    return str(checked(rec))
                for tile in r['tiles'].values():
                    for key,value in tile.items():
                        if key!='daily':
                            for field in mapped_fields(value):field['path']=restore(field['path'])
                for key in ('common_library','spatial_library','albedo_lut'):r[key]=restore(r[key])
                r['scheduler']['policy']=restore(r['scheduler']['policy'])
            if checkpoint:
                found=next((item for item in recoveries(root) if item[0]['generation']==checkpoint),None)
                if found is None:raise ValueError('Recovery checkpoint not retained')
                _,paths,old=found
                r['seed']={t:str(paths['checkpoint_'+t]) for t in r['tiles']}
                ends={row['checkpoint'][1] for path in r['seed'].values() for row in checkpoint_coverage(path)['stripes']}
                if len(ends)!=1:raise ValueError('Recovery tile dates disagree')
                r['start']=str(np.datetime64(ends.pop()+1,'D'))
                if r['start']>r['available_through']:raise ValueError('No dates after selected recovery checkpoint')
                from .nrt import source_identity
                from .nrt_storage import can_restart
                prior=expand(old,stop=str(np.datetime64(r['start'])-1))
                changed=[k.split('/')[1] for k,v in prior['inventory'].items() if k.split('/')[1]<r['start'] and source_identity(v)!=source_identity(complete['inventory'].get(k))]
                if changed:r['start']=str(min(np.datetime64(r['start']),np.datetime64(min(changed))-7))
                if r['start']<ledger['template']['start'] or not all(can_restart(checkpoint_coverage(path),r['start'],r['available_through'],ledger['history_start']) for path in r['seed'].values()):
                    raise ValueError('Recovery cannot rewind before superseded inputs with complete donor context; use full rebuild')
            request=directory/'request.json'
            if request.exists() and json.loads(request.read_text())!=r:raise FileExistsError('Different rebuild request already exists')
            atomic_json(request,r);scientific=plan(request,directory/'plan.json')
            update=dict(schema_version=1,root=str(root),expected_parent=g['generation'],plan=str(directory/'plan.json'),
                inventory=complete['inventory'],changed=[],changed_dates=[],replay_start=r['start'],through=r['available_through'],
                replay_mode='checkpoint' if checkpoint else 'full_history',state_identity=scientific['request']['state_identity'])
            update['identity']=digest(update);atomic_json(directory/'update.json',update)
            return update
        except Exception:
            workspace_state(root,directory,'failed');raise


def run(update_path):
    from .nrt import finish
    u=json.loads(Path(update_path).read_text());root=u['root'];directory=Path(update_path).parent
    with storage_lock(root):
        register_workspace(root,directory,'running')
        try:
            p=json.loads(Path(u['plan']).read_text())
            for stage in p['stage_order']:
                for i in range(len(p['tasks'][stage])):execute(u['plan'],stage,i)
            return finish(update_path)
        except Exception:
            workspace_state(root,directory,'failed');raise


def abandon(root,directory):
    """Explicitly release an abandoned queued-work pin; never expire it by age."""
    from .nrt_storage import lease_path
    with storage_lock(root,True):
        path=lease_path(root,directory)
        if not path.exists():raise ValueError('Workspace is not registered')
        workspace_state(root,directory,'abandoned')
        return dict(status='abandoned',directory=str(Path(directory).resolve()))


def catalog(root,destination,*,start=None,stop=None,publication=None):
    """Resolve current or as-issued products and observations from archived indexes."""
    from .nrt import current
    import re
    with storage_lock(root):
        if publication:
            if not re.fullmatch(r'generation-[A-Za-z0-9_-]+',publication):raise ValueError('Invalid publication identifier')
            path=Path(settings(root)['archive_root'])/'publications'/(publication+'.json')
            value=json.loads(path.read_text());generation=value['generation'];ledger=value['ledger']
        else:generation,_,ledger=current(root)
        expanded=expand(ledger,start=start,stop=stop)
        keep=lambda d:(start is None or d>=start) and (stop is None or d<=stop)
        result=dict(generation=generation['generation'],available_through=ledger['available_through'],
            inventory={k:v for k,v in expanded['inventory'].items() if keep(k.split('/')[1])},
            products={k:v for k,v in expanded['products'].items() if keep(k.split('/')[1])},
            daily={t:{d:v for d,v in fields['daily'].items() if keep(d)} for t,fields in expanded['template']['tiles'].items()})
        atomic_json(destination,result)
        return dict(output=str(Path(destination).resolve()),generation=result['generation'],observations=len(result['inventory']),products=len(result['products']))
