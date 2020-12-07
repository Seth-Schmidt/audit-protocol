from typing import Optional, Union
from fastapi import Depends, FastAPI, WebSocket, HTTPException, Security, Request, Response, BackgroundTasks, Cookie, \
    Query, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from eth_utils import keccak
from maticvigil.EVCore import EVCore
import logging
import sys
import json
import aioredis
import io
import redis
import time
import requests
import async_timeout
from skydb import SkydbTable
import ipfshttpclient
from config import settings

print(settings.as_dict())
ipfs_client = ipfshttpclient.connect()

formatter = logging.Formatter(u"%(levelname)-8s %(name)-4s %(asctime)s,%(msecs)d %(module)s-%(funcName)s: %(message)s")

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.DEBUG)
# stdout_handler.setFormatter(formatter)

stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.ERROR)
# stderr_handler.setFormatter(formatter)
rest_logger = logging.getLogger(__name__)
rest_logger.setLevel(logging.DEBUG)
rest_logger.addHandler(stdout_handler)
rest_logger.addHandler(stderr_handler)

# setup CORS origins stuff
origins = ["*"]

redis_lock = redis.Redis()

app = FastAPI(docs_url=None, openapi_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)
app.mount('/static', StaticFiles(directory='static'), name='static')

REDIS_CONN_CONF = {
    "host": settings['REDIS']['HOST'],
    "port": settings['REDIS']['PORT'],
    "password": settings['REDIS']['PASSWORD'],
    "db": settings['REDIS']['DB']
}
#
STORAGE_CONFIG = {
    "hot": {
        "enabled": True,
        "allowUnfreeze": True,
        "ipfs": {
            "addTimeout": 30
        }
    },
    "cold": {
        "enabled": True,
        "filecoin": {
            "repFactor": 1,
            "dealMinDuration": 518400,
            "renew": {
            },
            "addr": "placeholderstring"
        }
    }
}


@app.on_event('startup')
async def startup_boilerplate():
    app.redis_pool = await aioredis.create_pool(
        address=(REDIS_CONN_CONF['host'], REDIS_CONN_CONF['port']),
        db=REDIS_CONN_CONF['db'],
        password=REDIS_CONN_CONF['password'],
        maxsize=100
    )
    app.evc = EVCore(verbose=True)
    app.contract = app.evc.generate_contract_sdk(
        contract_address=settings.audit_contract,
        app_name='auditrecords'
    )


@app.post('/commit_payload')
async def commit_payload(
        request: Request,
        response: Response,
):
    """"
        - This endpoint accepts the payload and adds it to ipfs.
        - The cid retrieved after adding to ipfs is committed to a Smart Contract and once the transaction goes
        through a webhook listener will catch that event and update the DAG with the latest block along with the timestamp
        of when the cid of the payload was committed.
    """
    req_args = await request.json()
    try:
        payload = req_args['payload']
        project_id = req_args['projectId']
    except Exception as e:
        return {'error': "Either payload or projectId"}
    prev_dag_cid = ""
    prev_payload_cid = None
    block_height = 0
    ipfs_table = None
    redis_conn = None
    redis_conn_raw = None
    if settings.METADATA_CACHE == 'skydb':
        ipfs_table = SkydbTable(
            table_name=f"{settings.dag_table_name}:{project_id}",
            columns=['cid'],
            seed=settings.seed,
            verbose=1
        )
        if ipfs_table.index == 0:
            prev_dag_cid = ""
        else:
            prev_dag_cid = ipfs_table.fetch_row(row_index=ipfs_table.index - 1)['cid']
        block_height = ipfs_table.index

    elif settings.METADATA_CACHE == 'redis':
        """ Fetch the cid of latest DAG block along with the latest block height. """
        redis_conn_raw = await request.app.redis_pool.acquire()
        redis_conn = aioredis.Redis(redis_conn_raw)
        last_known_dag_cid_key = f'projectID:{project_id}:lastDagCid'
        r = await redis_conn.get(last_known_dag_cid_key)
        if r:
            """ Retrieve the height of the latest block only if the DAG projectId is not empty """
            prev_dag_cid = r.decode('utf-8')
            last_block_height_key = f'projectID:{project_id}:blockHeight'
            r2 = await redis_conn.get(last_block_height_key)
            if r2:
                block_height = int(r2)

    """ 
        - If the prev_dag_cid is empty, then it means that this is the first block that
        will be added to the DAG of the projectId.
    """
    if prev_dag_cid != "":
        prev_payload_cid = ipfs_client.dag.get(prev_dag_cid).as_json()['Data']['Cid']
    payload_changed = False

    rest_logger.debug('Previous IPLD CID in the DAG: ')
    rest_logger.debug(prev_dag_cid)

    """ The DAG will be created in the Webhook listener script """
    # dag = settings.dag_structure.to_dict()

    """ Add the Payload to IPFS """
    if type(payload) is dict:
        snapshot_cid = ipfs_client.add_json(payload)
    else:
        try:
            snapshot_cid = ipfs_client.add_str(str(payload))
        except:
            response.status_code = 400
            return {'success': False, 'error': 'PayloadNotSuppported'}
    rest_logger.debug('Payload CID')
    rest_logger.debug(snapshot_cid)
    snapshot = dict()
    snapshot['Cid'] = snapshot_cid
    snapshot['Type'] = "HOT_IPFS"
    """ Check if the payload has changed. """
    if prev_payload_cid:
        if prev_payload_cid != snapshot['Cid']:
            payload_changed = True
    rest_logger.debug(snapshot)

    ipfs_cid = snapshot['Cid']
    token_hash = '0x' + keccak(text=json.dumps(snapshot)).hex()
    async with async_timeout.timeout(5) as cm:
        try:
            tx_hash_obj = request.app.contract.commitRecord(**dict(
                ipfsCid=ipfs_cid,
                apiKeyHash=token_hash,
            ))
        except requests.exceptions.ConnectionError:
            tx_hash_obj = None
    if settings.METADATA_CACHE == 'redis':
        if not cm.expired and tx_hash_obj:
            """ Put this transaction hash on redis so that webhook listener can access it when listening to events"""
            hash_key = f"TRANSACTION:{tx_hash_obj[0]['txHash']}"
            hash_field = f"project_id"
            r = await redis_conn.hset(
                    key=hash_key,
                    field=hash_field,
                    value=f"{project_id}"
                )
            hash_field = f"tentative_block_height"
            r = await redis_conn.hset(
                    key=hash_key,
                    field=hash_field,
                    value=f"{block_height}"
                )

            hash_field = f"prev_dag_cid"
            r = await redis_conn.hset(
                    key=hash_key,
                    field=hash_field,
                    value=prev_dag_cid,
                )
        else:
            rest_logger.error('='*80)
            rest_logger.error('Commit Payload to timed out')
            rest_logger.error(ipfs_cid)
            rest_logger.error(project_id)
            rest_logger.error('=' * 80)
        request.app.redis_pool.release(redis_conn_raw)

    return {
        'Cid': snapshot['Cid'],
        'tentativeHeight': block_height,
        'payloadChanged': payload_changed,
    }


# TODO: get API key/token specific updates corresponding to projects committed with those credentials
@app.get('/projects/updates')
async def get_latest_project_updates(
        request: Request,
        response: Response,
        namespace: str = Query(default=None),
        maxCount: int = Query(default=20)
):
    project_diffs_snapshots = list()
    if settings.METADATA_CACHE == 'redis':
        redis_conn_raw = await request.app.redis_pool.acquire()
        redis_conn = aioredis.Redis(redis_conn_raw)
        h = await redis_conn.hgetall('auditprotocol:lastSeenSnapshots')
        if len(h) < 1:
            request.app.redis_pool.release(redis_conn_raw)
            return {}
        for i, d in h.items():
            project_id = i.decode('utf-8')
            diff_data = json.loads(d)
            each_project_info = {
                'projectId': project_id,
                'diff_data': diff_data
            }
            if namespace and namespace in project_id:
                project_diffs_snapshots.append(each_project_info)
            if not namespace:
                try:
                    project_id_int = int(project_id)
                except:
                    pass
                else:
                    project_diffs_snapshots.append(each_project_info)
        request.app.redis_pool.release(redis_conn_raw)
    sorted_project_diffs_snapshots = sorted(project_diffs_snapshots, key=lambda x: x['diff_data']['cur']['timestamp'], reverse=True)
    return sorted_project_diffs_snapshots[:maxCount]


@app.get('/{projectId:str}/payloads/cachedDiffs')
async def get_payloads_diffs(
        request: Request,
        response: Response,
        projectId: str,
        from_height: int = Query(default=0),
        to_height: int = Query(default=-1),
        maxCount: int = Query(default=10),
):
    max_block_height = 0
    if settings.METADATA_CACHE == 'redis':
        redis_conn_raw = await request.app.redis_pool.acquire()
        redis_conn = aioredis.Redis(redis_conn_raw)
        h = await redis_conn.get(f'projectID:{projectId}:blockHeight')
        if not h:
            max_block_height = 0
        else:
            max_block_height = int(h.decode('utf-8')) - 1

    if to_height == -1:
        to_height = max_block_height
    if (from_height < 0) or (to_height > max_block_height) or (from_height > to_height):
        return {'error': 'Invalid Height'}
    extracted_count = 0
    diff_response = list()
    if settings.METADATA_CACHE == 'redis':
        diff_snapshots_cache_zset = f'projectID:{projectId}:diffSnapshots'
        r = await redis_conn.zrevrangebyscore(
            key=diff_snapshots_cache_zset,
            min=from_height,
            max=to_height,
            withscores=False
        )
        request.app.redis_pool.release(redis_conn_raw)
        for diff in r:
            if extracted_count >= maxCount:
                break
            diff_response.append(json.loads(diff))
            extracted_count += 1
    return {
        'count': extracted_count,
        'diffs': diff_response
    }


@app.get('/{projectId:str}/payloads')
async def get_payloads(
        request: Request,
        response: Response,
        projectId: str,
        from_height: int = Query(None),
        to_height: int = Query(None),
        data: Optional[str] = Query(None)
):
    ipfs_table = None
    max_block_height = None
    redis_conn_raw = None
    redis_conn = None
    if settings.METADATA_CACHE == 'skydb':
        ipfs_table = SkydbTable(table_name=f"{settings.dag_table_name}:{projectId}",
                                columns=['cid'],
                                seed=settings.seed,
                                verbose=1)
        max_block_height = ipfs_table.index - 1
    elif settings.METADATA_CACHE == 'redis':
        redis_conn_raw = await request.app.redis_pool.acquire()
        redis_conn = aioredis.Redis(redis_conn_raw)
        h = await redis_conn.get(f'projectID:{projectId}:blockHeight')
        if not h:
            max_block_height = 0
        else:
            max_block_height = int(h.decode('utf-8')) - 1
    if data:
        if data.lower() == 'true':
            data = True
        else:
            data = False

    if (from_height < 0) or (to_height > max_block_height) or (from_height > to_height):
        return {'error': 'Invalid Height'}

    blocks = list()
    current_height = to_height
    prev_dag_cid = ""
    prev_payload_cid = None
    idx = 0
    while current_height >= from_height:
        rest_logger.debug("Fetching block at height: " + str(current_height))
        if not prev_dag_cid:
            if settings.METADATA_CACHE == 'skydb':
                prev_dag_cid = ipfs_table.fetch_row(row_index=current_height)['cid']
            elif settings.METADATA_CACHE == 'redis':
                project_cids_key_zset = f'projectID:{projectId}:Cids'
                r = await redis_conn.zrangebyscore(
                    key=project_cids_key_zset,
                    min=current_height,
                    max=current_height,
                    withscores=False
                )
                if r:
                    prev_dag_cid = r[0].decode('utf-8')
                else:
                    return {'error': 'NoRecordsFound'}
        block = ipfs_client.dag.get(prev_dag_cid).as_json()
        formatted_block = dict()
        formatted_block['dagCid'] = prev_dag_cid
        formatted_block.update({k: v for k, v in block.items()})
        formatted_block['prevDagCid'] = formatted_block.pop('prevCid')
        if data:
            formatted_block['Data']['payload'] = ipfs_client.cat(block['Data']['Cid']).decode()
        if prev_payload_cid:
            if prev_payload_cid != block['Data']['Cid']:
                blocks[idx-1]['payloadChanged'] = True
                diff_key = f"CidDiff:{prev_payload_cid}:{block['Data']['Cid']}"
                diff_b = await redis_conn.get(diff_key)
                diff_map = dict()
                if not diff_b:
                    # diff not cached already
                    rest_logger.debug('Diff not cached | New CID | Old CID')
                    rest_logger.debug(blocks[idx-1]['Data']['Cid'])
                    rest_logger.debug(block['Data']['Cid'])
                    if 'payload' in formatted_block['Data'].keys():
                        prev_data = formatted_block['Data']['payload']
                    else:
                        prev_data = ipfs_client.cat(block['Data']['Cid']).decode()
                    prev_data = json.loads(prev_data)
                    if 'payload' in blocks[idx-1]['Data'].keys():
                        cur_data = blocks[idx-1]['Data']['payload']
                    else:
                        cur_data = ipfs_client.cat(blocks[idx-1]['Data']['Cid']).decode()
                    cur_data = json.loads(cur_data)
                    # calculate diff
                    for k, v in cur_data.items():
                        if k not in prev_data.keys():
                            rest_logger.info('Ignoring key in older payload as it is not present')
                            rest_logger.info(k)
                            blocks[idx - 1]['payloadChanged'] = False
                            continue
                        if v != prev_data[k]:
                            diff_map[k] = {'old': prev_data[k], 'new': v}
                    if len(diff_map):
                        rest_logger.debug('Found diff in first time calculation')
                        rest_logger.debug(diff_map)
                        blocks[idx - 1]['payloadChanged'] = True
                    # cache in redis
                    await redis_conn.set(diff_key, json.dumps(diff_map))
                else:
                    diff_map = json.loads(diff_b)
                    rest_logger.debug('Found Diff in Cache! | New CID | Old CID | Diff')
                    rest_logger.debug(blocks[idx - 1]['Data']['Cid'])
                    rest_logger.debug(block['Data']['Cid'])
                    rest_logger.debug(diff_map)
                blocks[idx-1]['diff'] = diff_map
            else:
                blocks[idx-1]['payloadChanged'] = False
        prev_payload_cid = block['Data']['Cid']
        blocks.append(formatted_block)
        prev_dag_cid = formatted_block['prevDagCid']
        current_height = current_height - 1
        idx += 1
    if settings.METADATA_CACHE == 'redis':
        request.app.redis_pool.release(redis_conn_raw)
    return blocks


@app.get('/{projectId}/payloads/height')
async def payload_height(request: Request, response: Response, projectId: str):
    max_block_height = -1
    if settings.METADATA_CACHE == 'skydb':
        ipfs_table = SkydbTable(table_name=f"{settings.dag_table_name}:{projectId}",
                                columns=['cid'],
                                seed=settings.seed)
        max_block_height = ipfs_table.index - 1
    elif settings.METADATA_CACHE == 'redis':
        redis_conn_raw = await request.app.redis_pool.acquire()
        redis_conn = aioredis.Redis(redis_conn_raw)
        h = await redis_conn.get(f'projectID:{projectId}:blockHeight')
        if not h:
            max_block_height = 0
        else:
            max_block_height = int(h.decode('utf-8')) - 1
        rest_logger.debug(max_block_height)
        request.app.redis_pool.release(redis_conn_raw)

    return {"height": max_block_height}


@app.get('/{projectId}/payload/{block_height}')
async def get_block(request: Request,
                    response: Response,
                    projectId: str,
                    block_height: int,
                    ):
    if settings.METADATA_CACHE == 'skydb':
        ipfs_table = SkydbTable(table_name=f"{settings.dag_table_name}:{projectId}",
                                columns=['cid'],
                                seed=settings.seed,
                                verbose=1)

        if (block_height > ipfs_table.index - 1) or (block_height < 0):
            response.status_code = 400
            return {'error': 'Invalid block Height'}

            row = ipfs_table.fetch_row(row_index=block_height)
            block = ipfs_client.dag.get(row['cid']).as_json()
            return {row['cid']: block}
    elif settings.METADATA_CACHE == 'redis':
        redis_conn_raw = await request.app.redis_pool.acquire()
        redis_conn = aioredis.Redis(redis_conn_raw)
        max_block_height = await redis_conn.get(f"projectID:{projectId}:blockHeight")
        if not max_block_height:
            request.app.redis_pool.release(redis_conn_raw)
            response.status_code = 400
            return {'error': 'Block does not exist at this block height'}
        max_block_height = int(max_block_height.decode('utf-8'))-1
        rest_logger.debug(max_block_height)
        if block_height > max_block_height:
            request.app.redis_pool.release(redis_conn_raw)
            response.status_code = 400
            return {'error': 'Invalid Block Height'}

        project_cids_key_zset = f'projectID:{projectId}:Cids'
        r = await redis_conn.zrangebyscore(
            key=project_cids_key_zset,
            min=block_height,
            max=block_height,
            withscores=False
        )
        prev_dag_cid = r[0].decode('utf-8')
        block = ipfs_client.dag.get(prev_dag_cid).as_json()
        request.app.redis_pool.release(redis_conn_raw)
        return {prev_dag_cid: block}


@app.get('/{projectId:str}/payload/{block_height:int}/data')
async def get_block_data(
        request: Request,
        response: Response,
        projectId: str,
        block_height: int,
):
    if settings.METADATA_CACHE == 'skydb':
        ipfs_table = SkydbTable(table_name=f"{settings.dag_table_name}:{projectId}",
                                columns=['cid'],
                                seed=settings.seed,
                                verbose=1)
        if (block_height > ipfs_table.index - 1) or (block_height < 0):
            return {'error': 'Invalid block Height'}
        row = ipfs_table.fetch_row(row_index=block_height)
        block = ipfs_client.dag.get(row['cid']).as_json()
        block['Data']['payload'] = ipfs_client.cat(block['Data']['Cid']).decode()
        return {row['cid']: block['Data']}

    elif settings.METADATA_CACHE == "redis":
        redis_conn_raw = await request.app.redis_pool.acquire()
        redis_conn = aioredis.Redis(redis_conn_raw)
        max_block_height = await redis_conn.get(f"projectID:{projectId}:blockHeight")
        if not max_block_height:
            response.status_code = 400
            return {'error': 'Invalid Block Height'}
        max_block_height = int(max_block_height.decode('utf-8'))-1
        if block_height > max_block_height:
            response.status_code = 400
            return {'error': 'Invalid Block Height'}

        project_cids_key_zset = f'projectID:{projectId}:Cids'
        r = await redis_conn.zrangebyscore(
            key=project_cids_key_zset,
            min=block_height,
            max=block_height,
            withscores=False
        )
        prev_dag_cid = r[0].decode('utf-8')
        block = ipfs_client.dag.get(prev_dag_cid).as_json()
        payload = block['Data']
        payload['payload'] = ipfs_client.cat(block['Data']['Cid']).decode()
        request.app.redis_pool.release(redis_conn_raw)
        return {prev_dag_cid: payload}

