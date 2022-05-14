import logging

from utils import redis_keys
from httpx import AsyncClient, Timeout, Limits
from config import settings
from tenacity import AsyncRetrying, stop_after_attempt
import aioredis


async def get_tentative_block_height(
        project_id: str,
        reader_redis_conn: aioredis.Redis
) -> int:
    tentative_block_height_key = redis_keys.get_tentative_block_height_key(project_id=project_id)
    out: bytes = await reader_redis_conn.get(tentative_block_height_key)
    if out:
        tentative_block_height = int(out)
    else:
        tentative_block_height = 0
    return tentative_block_height


async def get_last_dag_cid(
        project_id: str,
        reader_redis_conn: aioredis.Redis
) -> str:
    last_dag_cid_key = redis_keys.get_last_dag_cid_key(project_id=project_id)
    out: bytes = await reader_redis_conn.get(last_dag_cid_key)
    if out:
        last_dag_cid = out.decode('utf-8')
    else:
        last_dag_cid = ""

    return last_dag_cid


async def get_dag_cid(
        project_id: str,
        block_height: int,
        reader_redis_conn: aioredis.Redis
):

    dag_cids_key = redis_keys.get_dag_cids_key(project_id)
    out = await reader_redis_conn.zrangebyscore(
        name=dag_cids_key,
        max=block_height,
        min=block_height,
        withscores=False
    )

    if out:
        if isinstance(out, list):
            out = out.pop()
        dag_cid = out.decode('utf-8')
    else:
        dag_cid = ""

    return dag_cid


async def get_last_payload_cid(
        project_id: str,
        reader_redis_conn: aioredis.Redis
):
    last_payload_cid_key = redis_keys.get_last_snapshot_cid_key(project_id=project_id)
    out: bytes = await reader_redis_conn.get(last_payload_cid_key)
    if out:
        last_payload_cid = out.decode('utf-8')
    else:
        last_payload_cid = ""

    return last_payload_cid


async def commit_payload(project_id, report_payload, session: AsyncClient):
    audit_protocol_url = f'http://{settings.host}:{settings.port}/commit_payload'
    async for attempt in AsyncRetrying(reraise=True, stop=stop_after_attempt(3)):
        with attempt:
            response_obj = await session.post(
                    url=audit_protocol_url,
                    json={'payload': report_payload, 'projectId': project_id}
            )
            logging.debug('Got audit protocol response: %s', response_obj.text)
            response_status_code = response_obj.status_code
            response = response_obj.json() or {}
            if response_status_code in range(200, 300):
                return response
            elif response_status_code == 500 or response_status_code == 502:
                return {
                    "message": f"failed with status code: {response_status_code}", "response": response
                }  # ignore 500 and 502 errors
            else:
                raise Exception(
                    'Failed audit protocol engine call with status code: {} and response: {}'.format(
                        response_status_code, response))


async def get_block_height(
        project_id: str,
        reader_redis_conn,
) -> int:
    block_height_key = redis_keys.get_block_height_key(project_id=project_id)
    out: bytes = await reader_redis_conn.get(block_height_key)
    if out:
        block_height = int(out)
    else:
        block_height = 0

    return block_height


async def get_last_pruned_height(
        project_id: str,
        reader_redis_conn
):
    last_pruned_key = redis_keys.get_last_pruned_key(project_id=project_id)
    out: bytes = await reader_redis_conn.get(last_pruned_key)
    if out:
        last_pruned_height: int = int(out.decode('utf-8'))
    else:
        last_pruned_height: int = 0
    return last_pruned_height


async def check_project_exists(
        project_id: str,
        reader_redis_conn: aioredis.Redis
):
    stored_projects_key = redis_keys.get_stored_project_ids_key()
    out = await reader_redis_conn.sismember(
        name=stored_projects_key,
        value=project_id
    )

    return out

