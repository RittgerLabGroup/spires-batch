"""Versioned NRT inventory, standard-product replacement and domain publication.

First/final are temporal issues, independent of input quality. Every unresolved
NRT date is revisited. Replacements replay causal state and cross-tile donors;
prior generations remain available as the as-issued record.
"""
import argparse
import copy
from datetime import date, timedelta
import json
from pathlib import Path
import re
import shutil

from .interpolation import (digest, file_record, plan as make_plan, load_plan, execute,
                            state_context, compatible_context)

NAME = re.compile(r'^(VJ109GA(?:_NRT)?)\.A(\d{4})(\d{3})\.(h\d{2}v\d{2})\.(\d{3})\.(\d+)\.h5$')


def source_record(path, *, prepared=None, prepared_context=None):
    rec = file_record(path)
    m = NAME.fullmatch(Path(path).name)
    if m is None:
        raise ValueError('Expected a NOAA-20 VJ109GA standard/NRT granule')
    product, year, doy, tile, collection, processing = m.groups()
    day = date(int(year),1,1)+timedelta(days=int(doy)-1)
    if day.year != int(year) or int(doy)<1:
        raise ValueError('Invalid granule day')
    rec.update(product=product, date=day.isoformat(), tile=tile, collection=collection,
               processing=processing, source_stream='nrt' if product.endswith('_NRT') else 'standard')
    if prepared is not None:
        rec.update(prepared=prepared,prepared_context=prepared_context)
    return rec


def source_identity(rec):
    if rec is None:return None
    return {k:rec[k] for k in ('path','bytes','mtime_ns','product','date','tile','collection','processing')}


def rank(rec):
    return (rec['source_stream']=='standard',int(rec['processing']),rec.get('cmr_updated',''))


def choose_sources(previous, candidates, *, tiles, collection, through, history_start):
    """Prefer standard over NRT, latest processing within the pinned collection.

    The last committed source participates in selection: temporary catalog gaps
    never remove observations or downgrade standard data back to NRT.
    """
    selected=copy.deepcopy(previous)
    for rec in candidates:
        actual=source_record(rec['path'])
        if any(actual[k]!=rec.get(k) for k in actual):
            raise ValueError('Candidate metadata changed or disagrees with its filename')
        if rec['tile'] not in tiles or rec['collection']!=collection:
            raise ValueError('Candidate outside configured domain/collection')
        if not history_start<=rec['date']<=through:continue
        key=rec['tile']+'/'+rec['date'];old=selected.get(key)
        if old is None or rank(rec)>rank(old):selected[key]=copy.deepcopy(rec)
        elif rank(rec)==rank(old):
            if source_identity(rec)==source_identity(old):
                if 'prepared' in rec:selected[key]=copy.deepcopy(rec)
            elif rec['path']==old['path']:
                selected[key]=copy.deepcopy(rec)  # Same granule was replaced in place.
            else:raise ValueError(f'Ambiguous equal-ranked source vintages: {key}')
    day=date.fromisoformat(history_start)
    while day<=date.fromisoformat(through):
        for tile in tiles:selected.setdefault(tile+'/'+day.isoformat(),None)
        day+=timedelta(days=1)
    changed=sorted(k for k in selected if source_identity(selected[k])!=source_identity(previous.get(k)))
    # Newly missing dates still advance calendar state and issue eligibility.
    changed+=sorted(k for k in selected if k not in previous and selected[k] is None)
    return selected,changed


def day_context(inventory,tiles,day):
    return digest({t:source_identity(inventory.get(t+'/'+day)) for t in sorted(tiles)})


def acquisition_dates(inventory, through, *, lookback_days=7):
    end=date.fromisoformat(through)
    days={(end-timedelta(days=i)).isoformat() for i in range(lookback_days)}
    days.update(k.split('/')[1] for k,v in inventory.items() if v is None or v['source_stream']=='nrt')
    return sorted(days)


def current(root):
    from spires_interpolation.publication import current_generation,artifact_paths
    generation=current_generation(root)
    if generation is None:raise ValueError('Bootstrap this stream from validated historical output first')
    paths=artifact_paths(root,generation)
    return generation,paths,json.loads(paths['ledger'].read_text())


def bootstrap(historical_plan, inventory, root):
    """Register existing history once, without recomputation or an annual reset."""
    from spires_interpolation.publication import current_generation,artifact_paths
    p=load_plan(historical_plan,compatible_state=True);r=p['request']
    tiles=sorted(r['tiles']);expected={t+'/'+d for t in tiles for d in r['tiles'][t]['daily'] if r['history_start']<=d<=r['available_through']}
    if set(inventory)!=expected:raise ValueError('Historical inventory must explicitly cover every tile/date')
    for key,rec in inventory.items():
        tile,day=key.split('/')
        if (r['tiles'][tile]['daily'][day] is None)!=(rec is None):raise ValueError('Inventory/prepared observation disagreement')
        if rec is not None:
            actual=source_record(rec['path'])
            if source_identity(rec)!=source_identity(actual) or actual['tile']!=tile or actual['date']!=day or rec.get('source_stream')!=actual['source_stream'] or actual['collection']!='002':raise ValueError('Historical source identity mismatch')
    products={};checkpoints={}
    for tile in tiles:
        folder=Path(r['output'])/'products'/tile
        generation=current_generation(folder)
        if generation is None or generation['request']!={'plan':p['identity'],'tile':tile}:raise ValueError('Historical output is not the planned generation')
        paths=artifact_paths(folder,generation);checkpoints[tile]=paths['checkpoints']
        for issue in ('first','final'):
            if issue in paths:
                import netCDF4
                with netCDF4.Dataset(paths[issue]) as ds:
                    import numpy as np
                    for i,day in enumerate(ds['time'][:]):
                        products[f'{tile}/{np.datetime64(int(day),"D")}/{issue}']=dict(file=file_record(paths[issue]),index=i)
    template=copy.deepcopy(r);template.pop('state_identity');template.pop('state_context',None);template.pop('seed',None)
    template['seed_plan']=str(Path(historical_plan).resolve())
    template['scheduler'].pop('snapshot',None)
    ledger=dict(schema_version=1,available_through=r['available_through'],history_start=r['history_start'],
        state_identity=r['state_identity'],state_context=state_context(r,p['inputs'],p['implementations']),
        template=template,inventory=inventory,products=products,
        unresolved_nrt=sorted(k for k,v in inventory.items() if v is not None and v['source_stream']=='nrt'))
    return commit(root,ledger,checkpoints,expected_parent=None,identity=dict(bootstrap=p['identity'],inventory=digest(inventory)))


def extend_annual_context(template,additions):
    """Add explicitly registered annual masks, retaining all original bindings."""
    result=copy.deepcopy(template)
    for tile,years in (additions or {}).items():
        if tile not in result['tiles']:raise ValueError(f'Annual mask outside tile domain: {tile}')
        previous=result['tiles'][tile]['bright_soil']
        for year,field in years.items():
            if not re.fullmatch(r'\d{4}',year):raise ValueError('Annual mask keys must be four-digit water years')
            field=copy.deepcopy(field);field['path']=str(Path(field['path']).resolve())
            if year in previous and previous[year]!=field:
                raise ValueError(f'Cannot replace a registered annual mask: {tile}/WY{year}')
            previous[year]=field
    return result


def ledger_context(ledger,seed_plan=None):
    if 'state_context' in ledger:return ledger['state_context']
    source=seed_plan or ledger['template'].get('seed_plan')
    if source is None:raise ValueError('Legacy stream requires its original seed_plan to validate existing state; no history recomputation is needed')
    p=load_plan(source,compatible_state=True)
    if p['request']['state_identity']!=ledger['state_identity']:
        raise ValueError('seed_plan does not describe this stream state')
    return state_context(p['request'],p['inputs'],p['implementations'])


def plan_update(root,candidates,through,directory,*,collection='002',annual_bright_soil=None,seed_plan=None):
    """Freeze a revision plan after upstream preparation of changed date domains.

    Prepared observations on each changed date must identify the complete selected
    same-day source context. This prevents mixing new cloud halos with stale donors.
    """
    import numpy as np
    generation,paths,ledger=current(root)
    r=extend_annual_context(ledger['template'],annual_bright_soil);tiles=sorted(r['tiles'])
    if seed_plan is not None:r['seed_plan']=str(Path(seed_plan).resolve())
    previous_context=ledger_context(ledger,seed_plan)
    through=str(np.datetime64(through,'D'))
    if through<ledger['available_through']:raise ValueError('Availability may not move backwards')
    inventory,changed=choose_sources(ledger['inventory'],candidates,tiles=tiles,collection=collection,through=through,history_start=ledger['history_start'])
    if not changed and through==ledger['available_through']:
        return dict(status='unchanged',generation=generation['generation'])
    changed_days=sorted({k.split('/')[1] for k in changed})
    if through>ledger['available_through']:
        changed_days+=list(map(str,np.arange(np.datetime64(ledger['available_through'])+1,np.datetime64(through)+1)))
    changed_days=sorted(set(changed_days))
    for day in changed_days:
        context=day_context(inventory,tiles,day)
        for tile in tiles:
            rec=inventory[tile+'/'+day]
            if rec is None:r['tiles'][tile]['daily'][day]=None;continue
            if 'prepared' not in rec or rec.get('prepared_context')!=context:
                raise ValueError(f'Prepare the selected same-day cloud/inversion context for every tile: {tile}/{day}; context={context}')
            r['tiles'][tile]['daily'][day]=rec['prepared']
    # Causal changes propagate forward to the current day; final look-ahead also
    # reaches seven days backwards, including already published final products.
    beginning=np.datetime64(r['start'])
    first=max(beginning,np.datetime64(changed_days[0])-7)
    r.update(start=str(first),stop=str(np.datetime64(through)+1),available_through=through,source_stream='mixed',
             seed={t:str(paths['checkpoint_'+t]) for t in tiles})
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=True)
    r['output']=str(directory/'work')
    # Select a retained prior generation when a replacement predates the current
    # rewind window. Never silently truncate causal state or the 45-day donors.
    import h5py
    from spires_interpolation.checkpoint import SnowCheckpoint,rewind_snow
    from spires_interpolation.restart import AgeHistory
    def usable(candidate):
        try:
            for t,path in candidate.items():
                with h5py.File(path) as h:
                    for g in h.values():
                        rewind_snow(SnowCheckpoint.load(g['checkpoint'],identity=h.attrs['state_identity']),first-1,history_start=ledger['history_start'])
                        AgeHistory.load(g['age_history'],first-1)
                        d=g['property_history_checkpoint/dates'][:].astype('datetime64[D]')
                        needed=max(np.datetime64(ledger['history_start']),min(first-21,np.datetime64(through)-65))
                        if d[0]>needed or d[-1]<first-1:return False
            return True
        except (ValueError,KeyError):return False
    if not usable(r['seed']):
        candidates=[]
        for manifest in (Path(root)/'generations').glob('*/manifest.json'):
            old=json.loads(manifest.read_text())
            from spires_interpolation.publication import artifact_paths
            old_paths=artifact_paths(root,old)
            old_ledger=json.loads(old_paths['ledger'].read_text())
            try:compatible_context(ledger_context(old_ledger,seed_plan),previous_context)
            except ValueError:continue
            # Its source prefix must equal the selected prefix through the anchor.
            if any(source_identity(v)!=source_identity(inventory.get(k)) for k,v in old_ledger['inventory'].items() if k.split('/')[1]<str(first)):continue
            seed={t:str(old_paths['checkpoint_'+t]) for t in tiles}
            if usable(seed):candidates.append((old['created_utc'],seed))
        if candidates:r['seed']=max(candidates,key=lambda x:x[0])[1]
        else:
            # Safe explicit fallback: full scientific prefix; also revises all
            # products from the stream's original target start. No changed
            # observations are ignored, including across water-year boundaries.
            r.pop('seed');r['start']=ledger['template']['start']
    request=directory/'request.json'
    if request.exists() and json.loads(request.read_text())!=r:raise FileExistsError('Revision directory already contains a different request')
    if not request.exists():
        with request.open('x') as stream:stream.write(json.dumps(r,indent=2)+'\n')
    scientific=make_plan(request,directory/'plan.json')
    compatible_context(previous_context,scientific['request']['state_context'])
    update=dict(schema_version=1,root=str(Path(root).resolve()),expected_parent=generation['generation'],plan=str(directory/'plan.json'),
        inventory=inventory,changed=changed,changed_dates=changed_days,replay_start=r['start'],through=through,
        replay_mode='checkpoint' if 'seed' in r else 'full_history',state_identity=scientific['request']['state_identity'])
    update['identity']=digest(update)
    path=directory/'update.json'
    if path.exists() and json.loads(path.read_text())!=update:raise FileExistsError('Revision directory already contains a different update')
    path.write_text(json.dumps(update,indent=2)+'\n')
    return update


def commit(root,ledger,checkpoints,*,expected_parent,identity):
    from spires_interpolation.publication import publish_generation
    def build(folder):
        outputs={}
        for tile,path in checkpoints.items():
            dest=folder/(tile+'_checkpoints.h5');shutil.copy2(path,dest);outputs['checkpoint_'+tile]=dest
        p=folder/'ledger.json';p.write_text(json.dumps(ledger,indent=2)+'\n');outputs['ledger']=p
        return outputs
    def validate(paths):
        import h5py
        for name,path in paths.items():
            if name.startswith('checkpoint_'):
                with h5py.File(path) as h:
                    if not h.attrs.get('complete') or h.attrs.get('state_identity')!=ledger['state_identity']:raise ValueError('Checkpoint context mismatch')
        checked={}
        for item in ledger['products'].values():
            rec=item['file']
            if rec['path'] not in checked:checked[rec['path']]=file_record(rec['path'])
            if checked[rec['path']]!=rec:raise ValueError('Product changed before domain commit')
    return publish_generation(root,request=identity,expected_parent=expected_parent,build=build,validate=validate)


def finish(update_path):
    import numpy as np,netCDF4
    from spires_interpolation.publication import current_generation,artifact_paths,StaleGeneration
    u=json.loads(Path(update_path).read_text());identity=u.pop('identity')
    if digest(u)!=identity:raise ValueError('Revision plan digest mismatch')
    generation,_,ledger=current(u['root'])
    if generation['request']==dict(update=identity):return dict(generation,reused=True)
    if generation['generation']!=u['expected_parent']:raise StaleGeneration('Another revision committed first')
    p=load_plan(u['plan']);r=p['request'];checkpoints={}
    for tile in r['tiles']:
        folder=Path(r['output'])/'products'/tile;g=current_generation(folder)
        if g is None or g['request']!={'plan':p['identity'],'tile':tile}:raise ValueError('Every donor-domain tile must finish before publication')
        paths=artifact_paths(folder,g);checkpoints[tile]=paths['checkpoints']
        for issue in ('first','final'):
            if issue not in paths:continue
            with netCDF4.Dataset(paths[issue]) as ds:
                for index,day in enumerate(ds['time'][:]):
                    ledger['products'][f'{tile}/{np.datetime64(int(day),"D")}/{issue}']=dict(file=file_record(paths[issue]),index=index)
    ledger.update(inventory=u['inventory'],available_through=u['through'],last_changed=u['changed'],last_replay_start=u['replay_start'],
                  state_identity=r['state_identity'],state_context=r['state_context'],
                  unresolved_nrt=sorted(k for k,v in u['inventory'].items() if v is not None and v['source_stream']=='nrt'))
    # Retain the full prepared-input map for later replacements and missing days.
    ledger['template']['tiles']=r['tiles']
    if 'seed_plan' in r:ledger['template']['seed_plan']=r['seed_plan']
    return commit(u['root'],ledger,checkpoints,expected_parent=u['expected_parent'],identity=dict(update=identity))


def main(argv=None):
    p=argparse.ArgumentParser(prog='spires-batch nrt');sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('bootstrap');b.add_argument('plan');b.add_argument('inventory');b.add_argument('--root',required=True)
    q=sub.add_parser('plan');q.add_argument('candidates');q.add_argument('--root',required=True);q.add_argument('--through',required=True);q.add_argument('--output',required=True)
    q.add_argument('--annual-bright-soil',help='JSON tile -> water year -> field additions; existing masks are retained')
    q.add_argument('--seed-plan',help='Original plan provenance for a legacy stream; does not rebuild history')
    f=sub.add_parser('finish');f.add_argument('update')
    r=sub.add_parser('run');r.add_argument('update')
    a=sub.add_parser('acquire');a.add_argument('--root',required=True);a.add_argument('--through',required=True);a.add_argument('--output',required=True);a.add_argument('--download',action='store_true')
    a=sub.add_parser('prepare');a.add_argument('request');a.add_argument('--output',required=True);a.add_argument('--receipt',required=True)
    a=sub.add_parser('retrieve');a.add_argument('request');a.add_argument('--output',required=True);a.add_argument('--receipt',required=True)
    a=sub.add_parser('cycle');a.add_argument('config');a.add_argument('--through',required=True);a.add_argument('--output',required=True);a.add_argument('--execute',action='store_true')
    s=sub.add_parser('status');s.add_argument('--root',required=True)
    a=p.parse_args(argv)
    if a.command=='cycle':
        from .pipeline import cycle
        result=cycle(a.config,a.through,a.output,run=a.execute)
    elif a.command=='retrieve':
        from .retrieval import retrieve
        result=retrieve(a.request,a.output)
        Path(a.receipt).write_text(json.dumps(result,indent=2)+'\n')
    elif a.command=='acquire':
        from .acquisition import acquire
        result=acquire(a.root,a.through,a.output,fetch=a.download)
    elif a.command=='prepare':
        from .inputs import prepare_retained
        result=prepare_retained(a.request,a.output)
        Path(a.receipt).write_text(json.dumps(result,indent=2)+'\n')
    elif a.command=='bootstrap':result=bootstrap(a.plan,json.loads(Path(a.inventory).read_text()),a.root)
    elif a.command=='plan':result=plan_update(a.root,json.loads(Path(a.candidates).read_text()),a.through,a.output,
        annual_bright_soil=json.loads(Path(a.annual_bright_soil).read_text()) if a.annual_bright_soil else None,seed_plan=a.seed_plan)
    elif a.command in ('finish','run'):
        if a.command=='run':
            u=json.loads(Path(a.update).read_text());plan=load_plan(u['plan'])
            for stage in plan['stage_order']:
                for i in range(len(plan['tasks'][stage])):execute(u['plan'],stage,i)
        result=finish(a.update)
    else:
        generation,_,ledger=current(a.root)
        result=dict(generation=generation['generation'],available_through=ledger['available_through'],unresolved_nrt=ledger['unresolved_nrt'],
                    acquisition_dates=acquisition_dates(ledger['inventory'],ledger['available_through']))
    summary={k:v for k,v in result.items() if k not in ('inventory','entries','candidates')}
    if 'inventory' in result:summary['inventory_count']=len(result['inventory'])
    print(json.dumps(summary,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
