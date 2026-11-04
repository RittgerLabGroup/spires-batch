"""CMR discovery and authenticated, atomic VJ109GA standard/NRT acquisition."""
import argparse
from datetime import date,timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlencode,urlparse
from urllib.request import Request,urlopen

from .nrt import NAME,source_record,acquisition_dates,current


def discover(days,tiles,*,collection='002'):
    """Query every requested day, including overdue NRT replacements.

    HTTP/catalog failures raise; they are not interpreted as missing observations.
    CMR paging is exhausted for both pinned products before returning a snapshot.
    """
    results=[]
    # Coalesce consecutive dates so unresolved history does not cost one request
    # per scene. Actual output is still filtered against the requested date set.
    wanted=set(days)
    if not wanted:return []
    ordered=sorted(wanted);ranges=[];start=previous=date.fromisoformat(ordered[0])
    for value in ordered[1:]:
        day=date.fromisoformat(value)
        if day>previous+timedelta(days=1):ranges.append((start,previous));start=day
        previous=day
    ranges.append((start,previous))
    for product in ('VJ109GA','VJ109GA_NRT'):
        for start,stop in ranges:
            # LANCE's catalog version is not the filename's collection, and its
            # temporal ranges can overlap adjacent acquisition days. Filter the
            # exact collection/day from the filename after a padded NRT query.
            params=dict(short_name=product,temporal=f'{start-timedelta(days=1)}T00:00:00Z,{stop+timedelta(days=2)}T23:59:59Z',page_size=2000)
            if product=='VJ109GA':params['version']=collection
            after=None
            while True:
                headers={'Accept':'application/json'}
                if after:headers['CMR-Search-After']=after
                req=Request('https://cmr.earthdata.nasa.gov/search/granules.json?'+urlencode(params),headers=headers)
                with urlopen(req,timeout=120) as response:
                    data=json.load(response);next_after=response.headers.get('CMR-Search-After')
                entries=data.get('feed',{}).get('entry',[])
                for entry in entries:
                    name=(entry.get('producer_granule_id') or entry.get('title','')).removesuffix('.h5')+'.h5'
                    m=NAME.fullmatch(name)
                    if not m or m[1]!=product or m[4] not in tiles or m[5]!=collection:continue
                    day=(date(int(m[2]),1,1)+timedelta(days=int(m[3])-1)).isoformat()
                    if day not in wanted:continue
                    links=[a['href'] for a in entry.get('links',[]) if a.get('href') and not a.get('inherited') and urlparse(a['href']).path.endswith('.h5') and 'opendap' not in a['href'].lower()]
                    if not links:continue
                    links.sort(key=lambda x:'/lp-prod-protected/' not in x)
                    results.append(dict(name=name,date=day,tile=m[4],product=product,collection=collection,processing=m[6],url=links[0],
                                        cmr_id=entry.get('id'),cmr_updated=entry.get('updated','')))
                if not entries or not next_after or next_after==after:break
                after=next_after
    return sorted(results,key=lambda v:(v['date'],v['tile'],v['product'],v['processing'],v['cmr_updated']))


def download(entry,directory):
    """Use existing Earthdata netrc credentials without reading/logging secrets."""
    import h5py
    url=entry['url'];parsed=urlparse(url)
    if parsed.scheme!='https' or not (parsed.hostname or '').endswith('.nasa.gov'):
        raise ValueError('CMR download must be an HTTPS NASA endpoint')
    if not NAME.fullmatch(entry['name']):raise ValueError('Invalid target granule filename')
    version=hashlib.sha256(json.dumps(entry,sort_keys=True).encode()).hexdigest()[:16]
    folder=Path(directory).resolve()/version;folder.mkdir(parents=True,exist_ok=True)
    destination=folder/entry['name']
    if not destination.exists():
        with tempfile.TemporaryDirectory(prefix='.download-',dir=folder) as temporary:
            temporary=Path(temporary);part=temporary/entry['name'];cookies=temporary/'cookies'
            subprocess.run(['curl','--fail','--silent','--show-error','--location','--netrc','--cookie',str(cookies),'--cookie-jar',str(cookies),
                            '--connect-timeout','60','--max-time','1200','--retry','2','--output',str(part),'--',url],check=True)
            # A successful login HTML response must never become a reflectance file.
            with h5py.File(part,'r') as h:
                if 'HDFEOS/GRIDS/VIIRS_Grid_500m_2D/Data Fields' not in h:raise ValueError('Downloaded response is not a VIIRS scene')
            part.replace(destination)
    with h5py.File(destination,'r') as h:
        if 'HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields' not in h:raise ValueError('Invalid cached VIIRS scene')
    rec=source_record(destination);rec.update(cmr_id=entry['cmr_id'],cmr_updated=entry['cmr_updated'])
    return rec


def acquire(root,through,directory,*,collection='002',fetch=False):
    _,_,ledger=current(root)
    days=acquisition_dates(ledger['inventory'],through)
    # Include an arbitrarily long interruption since the last successful cycle.
    start=date.fromisoformat(ledger['available_through'])+timedelta(days=1)
    while start<=date.fromisoformat(through):days.append(start.isoformat());start+=timedelta(days=1)
    entries=discover(sorted(set(days)),sorted(ledger['template']['tiles']),collection=collection)
    chosen={}
    for entry in entries:
        key=entry['tile']+'/'+entry['date']
        value=(entry['product']=='VJ109GA',int(entry['processing']),entry['cmr_updated'])
        if key not in chosen or value>chosen[key][0]:chosen[key]=(value,entry)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    result=dict(through=through,queried_dates=sorted(set(days)),entries=[item[1] for item in chosen.values()])
    if fetch:
        result['candidates']=[];result['reused_sources']=0
        for entry in result['entries']:
            old=ledger['inventory'].get(entry['tile']+'/'+entry['date'])
            # Reuse the committed granule across cycle workspaces. A new cache
            # path alone is not a new scientific input, nor an ambiguous vintage.
            old_rank=None if old is None else (old['source_stream']=='standard',int(old['processing']))
            new_rank=(entry['product']=='VJ109GA',int(entry['processing']))
            unchanged=old is not None and (old_rank>new_rank or (
                old_rank==new_rank and (not old.get('cmr_updated') or entry['cmr_updated']<=old['cmr_updated'])))
            if unchanged:
                actual=source_record(old['path'])
                if any(actual[k]!=old.get(k) for k in actual):raise ValueError('Committed native source changed in place')
                rec=dict(old);result['reused_sources']+=1
            else:rec=download(entry,directory/'reflectance')
            result['candidates'].append(rec)
    (directory/'acquisition.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--through',required=True);p.add_argument('--output',required=True);p.add_argument('--download',action='store_true')
    a=p.parse_args(argv);r=acquire(a.root,a.through,a.output,fetch=a.download)
    print(json.dumps(dict(queried_dates=r['queried_dates'],selected=len(r['entries']),downloaded=len(r.get('candidates',[]))),indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
