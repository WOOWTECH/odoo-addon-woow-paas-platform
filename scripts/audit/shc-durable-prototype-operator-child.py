"""Actual operator app/router/provider adapter, synthetic config, localhost TCP only."""
import os, sys
os.environ['API_KEY']='synthetic-only-key'
os.environ['SHC_DURABLE_PROVISION_PROTOTYPE_ENABLED']='true'
os.environ['CLOUDFLARE_API_TOKEN']='synthetic-provider-key'
os.environ['CLOUDFLARE_ACCOUNT_ID']='synthetic-account'
os.environ['CLOUDFLARE_DOMAIN']='shc-prototype.invalid'
os.environ['CLOUDFLARE_ZONE_ID']='synthetic-zone'
os.environ['CLOUDFLARE_ENABLED']='false'
import httpx
original=httpx.AsyncHTTPTransport.handle_async_request
async def localhost_only(self,request):
    if request.url.host not in {'127.0.0.1','localhost'}:
        raise RuntimeError('QA operator fixture forbids non-loopback HTTP transport')
    return await original(self,request)
httpx.AsyncHTTPTransport.handle_async_request=localhost_only
from src.main import app
from src.api import tunnels
from src.config import settings
settings.api_key='synthetic-only-key';settings.shc_durable_provision_prototype_enabled=True
provider=tunnels.cloudflare_service
provider.BASE_URL=sys.argv[1]+'/client/v4'
provider.api_token='synthetic-provider-key';provider.account_id='synthetic-account';provider.zone_id='synthetic-zone';provider.domain='shc-prototype.invalid'
import uvicorn
uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[2]),lifespan='off',access_log=False,log_level='warning')
