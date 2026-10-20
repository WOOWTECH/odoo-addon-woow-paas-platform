import assert from "node:assert/strict";
import test from "node:test";
import { webcrypto } from "node:crypto";
import { readPaas, stubImports, toDataUrl } from "./lib/llm_test_support.mjs";

globalThis.crypto ??= webcrypto;
globalThis.__creditApi = {};
globalThis.__creditWallet = 0;
globalThis.__creditUnmount = [];
const source = stubImports(await readPaas("components/llm/LlmConvertModal.js"), {
  "@odoo/owl": "class Component {} const onMounted=()=>{}; const onWillUnmount=fn=>globalThis.__creditUnmount.push(fn); const useRef=()=>({el:null}); const useState=x=>x;",
  "@web/core/l10n/translation": "const _t=(s,p={})=>s.replace(/%\\((\\w+)\\)s/g,(_,k)=>p[k]);",
  "../../services/llm_service": "const llmService=new Proxy({}, {get:(_,k)=>globalThis.__creditApi[k]}); const newClientRequestId=()=>globalThis.crypto.randomUUID();",
  "../../services/llm_errors": "const LLM_NETWORK_ERROR='llm_network_error'; const llmErrorMessage=r=>r.code;",
  "../../services/billing_service": "const billingService={fetchWallet:async()=>globalThis.__creditWallet++};",
  "../../utils/modal_focus": "const inertOutside=()=>()=>{}; const trapTab=()=>{};",
  "./LlmTermsModal": "class LlmTermsModal {}",
});
const { LlmConvertModal, parseCreditPoints, creditOrderText, creditPollDelay } = await import(toDataUrl(source));
const ok = data => ({ success:true, data });
function modal(overrides={}) {
  globalThis.__creditApi = {
    overview:{account:{state:"active",tos:{blocking:false}}},
    previewCredit:async points=>ok({wallet_balance:2000,balance_after:2000-points,quota_after_points:points+20,reserve:{needed_7d:0,short_by:0},pending_review:false}),
    convertCredit:async()=>ok({order_id:7,state:"paid"}),
    fetchCreditOrder:async()=>ok({id:7,state:"credited"}),
    findCreditOrder:async()=>ok({items:[]}),
    reauth:async()=>ok({reauth_until:"later"}),
    fetchState:async()=>ok({}), ...overrides,
  };
  const m=new LlmConvertModal(); m.props={onClose:()=>{}}; m.setup(); return m;
}
async function ready(overrides={}) {const m=modal(overrides); await m.loadPreview(); m.state.acceptNoRefund=true; return m;}

test("strict whole-point range and four presets",()=>{
  for(const bad of ["", "49", "10001", "50.1", "1e2", "0x64", true, 100.1]) assert.equal(parseCreditPoints(bad),null);
  assert.equal(parseCreditPoints("50"),50); assert.equal(parseCreditPoints("10000"),10000);
  assert.deepEqual(modal().quickPoints,[100,300,500,1000]);
});
test("new modal gets UUID; same flow and double click use one request",async()=>{
  let resolve; const calls=[];
  const otherId=modal().clientRequestId;
  const m=await ready({convertCredit:p=>{calls.push(p); return new Promise(r=>resolve=r);}});
  assert.notEqual(m.clientRequestId,otherId);
  const first=m.submit(); await m.submit(); assert.equal(calls.length,1);
  assert.equal(calls[0].client_request_id,m.clientRequestId);
  resolve(ok({order_id:7,state:"paid"})); await first; m.teardown();
});
test("no-refund acknowledgement and reserve are separate gates",async()=>{
  const m=modal(); await m.loadPreview(); assert.equal(m.canSubmit,false);
  m.state.acceptNoRefund=true; assert.equal(m.canSubmit,true);
  m.state.preview.reserve={needed_7d:1900,short_by:100}; assert.equal(m.canSubmit,false);
  m.state.confirmReserve=true; assert.equal(m.canSubmit,true);
  await m.setPoints("300"); assert.equal(m.state.confirmReserve,false);
});
test("preview stale responses cannot authorize a different amount",async()=>{
  let resolve; const m=modal({previewCredit:()=>new Promise(r=>resolve=r)});
  const first=m.loadPreview(); m.state.points="invalid"; await m.loadPreview();
  resolve(ok({balance_after:999,reserve:{short_by:0}})); await first;
  assert.equal(m.state.preview,null); assert.equal(m.canSubmit,false);
});
test("terms and pending review block conversion while preview remains readable",async()=>{
  const m=await ready(); globalThis.__creditApi.overview.account.tos.blocking=true;
  assert.equal(m.canSubmit,false); assert.ok(m.state.preview);
  globalThis.__creditApi.overview.account.tos.blocking=false; m.state.preview.pending_review=true;
  assert.equal(m.canSubmit,false);
});
test("over 1000 requires reauth and password clears before await",async()=>{
  let called=false;
  const m=await ready({reauth:async password=>{called=password; assert.equal(m.passwordRef.el.value,""); return ok({reauth_until:"later"});}});
  await m.setPoints("1500"); m.state.acceptNoRefund=true; assert.equal(m.canSubmit,false);
  m.passwordRef.el={value:"private"}; await m.verifyIdentity();
  assert.equal(called,"private"); assert.equal(m.canSubmit,true); assert.equal("password" in m.state,false); m.teardown();
});
test("timeout exact lookup finds original; accepted and voided refresh wallet",async()=>{
  globalThis.__creditWallet=0; const calls=[];
  const m=await ready({convertCredit:async()=>({success:false,code:"llm_network_error"}),findCreditOrder:async id=>{calls.push(id);return ok({items:[{id:8,state:"paid"}]});}});
  await m.submit(); assert.deepEqual(calls,[m.clientRequestId]); assert.equal(m.state.order.id,8);
  assert.equal(globalThis.__creditWallet,1); await m.acceptOrder({id:8,state:"voided"});
  assert.equal(globalThis.__creditWallet,2); m.teardown();
});
test("lookup failure stays confirming; only successful exact miss allows same-ID resend",async()=>{
  const m=await ready({convertCredit:async()=>({success:false,code:"llm_network_error"}),findCreditOrder:async()=>({success:false,code:"llm_network_error"})});
  const id=m.clientRequestId; await m.submit(); assert.equal(m.state.phase,"confirming"); assert.equal(m.canSubmit,false);
  globalThis.__creditApi.findCreditOrder=async()=>ok({items:[]}); await m.recoverOrder();
  assert.equal(m.state.phase,"form"); assert.equal(m.clientRequestId,id); assert.equal(m.canSubmit,true); m.teardown();
});
test("definite server errors never claim unknown execution",async()=>{
  const m=await ready({convertCredit:async()=>({success:false,code:"llm_tos_outdated"})});
  await m.submit(); assert.equal(m.state.phase,"form"); assert.equal(m.state.termsBlocked,true); assert.equal(m.canSubmit,false);
});
test("bounded polling, in-flight unmount safety and terminal text",async()=>{
  assert.equal(creditPollDelay(1),1000); assert.equal(creditPollDelay(999999),30000);
  const m=await ready(); await m.submit(); assert.ok(m.pollTimer); m.teardown(); assert.equal(m.pollTimer,null);
  assert.notEqual(creditOrderText("paid"),creditOrderText("credited"));
  assert.match(creditOrderText("unknown"),/15/); assert.match(creditOrderText("needs_review"),/not be lost/);
  let resolve; const n=await ready({fetchCreditOrder:()=>new Promise(r=>resolve=r)});
  n.state.order={id:7,state:"paid"}; const polling=n.pollOrder(); n.teardown(); resolve(ok({id:7,state:"credited"})); await polling;
  assert.equal(n.state.order.state,"paid"); assert.equal(n.pollTimer,null);
});
test("service sends exact lookup and reauth locally; no private errors survive",async()=>{
  const calls=[]; globalThis.__creditRpc=async(...args)=>{calls.push(args);return ok({items:[]});};
  const s=stubImports(await readPaas("services/llm_service.js"),{
    "@odoo/owl":"const reactive=x=>x;",
    "./rpc":"const jsonRpc=(...args)=>globalThis.__creditRpc(...args);",
    "./llm_errors":"const LLM_KEY_CREATE_INTERRUPTED='interrupted',LLM_NETWORK_ERROR='llm_network_error',LLM_SESSION_EXPIRED='expired';",
  });
  const {llmService}=await import(toDataUrl(s));
  await llmService.findCreditOrder("request"); await llmService.reauth("private"); await llmService.fetchCreditOrder(8);
  assert.deepEqual(calls.map(x=>x.slice(0,2)),[["/api/llm/credit/orders",{client_request_id:"request",page:1,limit:1}],["/api/llm/account/reauth",{password:"private"}],["/api/llm/credit/orders/8",{}]]);
  assert.equal("password" in llmService,false);
  globalThis.__creditRpc=async()=>({success:false,code:"llm_reserve_confirm_required",needed_7d:200,short_by:2,error:"private"});
  assert.deepEqual(await llmService.previewCredit(100),{success:false,code:"llm_reserve_confirm_required",needed_7d:200,short_by:2});
});

test("timeout retry preserves original amount; updated terms resume same flow",async()=>{
  const m=await ready({convertCredit:async()=>({success:false,code:"llm_network_error"})});
  const id=m.clientRequestId; await m.submit(); await m.setPoints("500");
  assert.equal(m.points,100); assert.equal(m.clientRequestId,id);
  m.state.termsBlocked=true; await m.onTermsClose({accepted:true});
  assert.equal(m.state.termsBlocked,false); assert.equal(m.clientRequestId,id); m.teardown();
});
test("automatic checks stop at count or deadline; manual check remains possible",async()=>{
  const m=modal(); m.pollCount=90; m.schedulePoll(()=>{},1000); assert.equal(m.pollTimer,null);
  m.pollCount=0; m.pollStarted=Date.now()-900001; m.schedulePoll(()=>{},1000); assert.equal(m.pollTimer,null);
  m.teardown();
});
const formatSource=stubImports(await readPaas("utils/llm_format.js"), {
  "@web/core/l10n/translation":"const _t=(s,p={})=>s.replace(/%\\((\\w+)\\)s/g,(_,k)=>p[k]);",
});
globalThis.__creditFormat=await import(toDataUrl(formatSource));
async function creditTab(overrides={}) {
  globalThis.__creditApi={overview:{account:{state:"active",balance_quota:390625}},fetchState:async()=>ok({}),fetchCreditOrders:async()=>ok({items:[{id:7,state:"sending",created_at:"2026-10-18T04:00:00Z"}],total:1}),...overrides};
  const s=stubImports(await readPaas("pages/llm/tabs/LlmCreditTab.js"),{
    "@odoo/owl":"class Component {} const useState=x=>x; const onMounted=()=>{}; const onWillUnmount=fn=>globalThis.__creditUnmount.push(fn);",
    "@web/core/l10n/translation":"const _t=(s,p={})=>s.replace(/%\\((\\w+)\\)s/g,(_,k)=>p[k]);",
    "../../../services/billing_service":"const billingService={fetchWallet:async()=>globalThis.__creditWallet++};",
    "../../../services/llm_service":"const llmService=new Proxy({}, {get:(_,k)=>globalThis.__creditApi[k]});",
    "../../../services/llm_errors":"const llmErrorMessage=r=>r.code;",
    "../../../utils/llm_format":"const {formatTaipeiDateTime,formatAiQuota}=globalThis.__creditFormat;",
    "../../../components/llm/LlmConvertModal":"class LlmConvertModal {} const creditOrderText=s=>s; const CREDIT_PENDING_STATES=['paid','sending','unknown']; const creditPollDelay=()=>1000;",
  });
  const {LlmCreditTab}=await import(toDataUrl(s)); const tab=new LlmCreditTab(); tab.setup(); return tab;
}
test("credit tab uses actual account DTO; pending orders poll and unload cancels",async()=>{
  const tab=await creditTab();
  assert.equal(tab.blockedText,""); assert.equal(tab.balanceText,"AI credit: 25.00 pts");
  globalThis.__creditApi.overview.pending_review=true; assert.equal(tab.blockedText,"llm_pending_review"); tab.openConvert(); assert.equal(tab.state.showConvert,false);
  globalThis.__creditApi.overview.pending_review=false; globalThis.__creditApi.overview.account.state="suspended"; assert.equal(tab.blockedText,"llm_suspended");
  globalThis.__creditWallet=0; await tab.load(); assert.equal(globalThis.__creditWallet,1); await tab.load(); assert.equal(globalThis.__creditWallet,1);
  globalThis.__creditApi.fetchCreditOrders=async()=>ok({items:[{id:7,state:"voided"}],total:1});
  await tab.load(); assert.equal(globalThis.__creditWallet,2); assert.equal(tab.pollTimer,null);
  globalThis.__creditApi.fetchCreditOrders=async()=>ok({items:[{id:7,state:"sending",created_at:"2026-10-18T04:00:00Z"}],total:1});
  await tab.load(); assert.ok(tab.pollTimer); assert.equal(tab.rows[0].time,"2026-10-18 12:00");
  globalThis.__creditUnmount.at(-1)(); assert.equal(tab.pollTimer,null);
});
test("credit tab formats fractional and negative balances from integer quota",async()=>{
  const tab=await creditTab();
  globalThis.__creditApi.overview.account={state:"active",balance_quota:171874,balance_points:10};
  assert.equal(tab.balanceText,"AI credit: 10.99 pts");
  globalThis.__creditApi.overview.account={state:"active",balance_quota:-1,balance_points:0,overdraft_points:1};
  assert.equal(tab.balanceText,"AI credit: -0.01 pts");
  globalThis.__creditApi.overview.account={state:"active",balance_points:0};
  assert.equal(tab.balanceText,"AI credit is not available right now.");
  tab.clearPoll();
});
test("order transitions refresh AI balance and release the pending-review gate",async()=>{
  let fetches=0;
  const tab=await creditTab({fetchState:async()=>{
    fetches++;
    if(fetches>1) globalThis.__creditApi.overview={account:{state:"active",balance_quota:546874},pending_review:false};
    return ok({});
  }});
  globalThis.__creditApi.overview.pending_review=true;
  await tab.load(); assert.equal(fetches,1); assert.equal(tab.blockedText,"llm_pending_review");
  await tab.load(); assert.equal(fetches,1,"unchanged automatic poll avoids redundant state calls");
  globalThis.__creditApi.fetchCreditOrders=async()=>ok({items:[{id:7,state:"credited"}],total:1});
  await tab.load(); assert.equal(fetches,2); assert.equal(tab.balanceText,"AI credit: 34.99 pts"); assert.equal(tab.blockedText,"");
  assert.equal(tab.pollTimer,null);
});
test("manual Refresh fetches account state even with unchanged or unavailable order history",async()=>{
  let fetches=0; const tab=await creditTab({fetchState:async()=>{fetches++;return ok({});}});
  await tab.load(); assert.equal(fetches,1);
  await tab.refresh(); assert.equal(fetches,2);
  globalThis.__creditApi.fetchCreditOrders=async()=>({success:false,code:"llm_unavailable"});
  await tab.refresh(); assert.equal(fetches,3); assert.equal(tab.state.error,"llm_unavailable"); tab.clearPoll();
});
test("conversion history marks its formatted timestamps as Taipei time",async()=>{
  const template=await readPaas("pages/llm/tabs/LlmCreditTab.xml");
  assert.match(template,/<dt>Time \(Taipei\)<\/dt>/);
});

test("preview warns and blocks when the points wallet cannot cover conversion",async()=>{
  const m=await ready(); m.state.preview.balance_after=-1; assert.equal(m.insufficientBalance,true); assert.equal(m.canSubmit,false); m.teardown();
});


test("idle reauth expiry clears reactive eligibility and reopens identity controls",async t=>{
  const started=Date.UTC(2026,9,18,12);
  t.mock.timers.enable({apis:["setTimeout","Date"],now:started});
  const m=await ready({reauth:async()=>ok({reauth_until:new Date(started+600000).toISOString()})});
  t.after(()=>m.teardown());
  await m.setPoints("1500"); m.passwordRef.el={value:"private"}; await m.verifyIdentity();
  assert.equal(m.needsReauth,false); assert.equal(m.canSubmit,true);
  t.mock.timers.tick(599999); assert.equal(m.state.verifiedUntil,started+600000);
  t.mock.timers.tick(1);
  assert.equal(m.state.verifiedUntil,0,"timer must mutate reactive state without user interaction");
  assert.equal(m.needsReauth,true); assert.equal(m.canSubmit,false); assert.equal(m.reauthTimer,null);
});
test("closing or unmounting clears the reauth expiry timer",async t=>{
  t.mock.timers.enable({apis:["setTimeout","Date"],now:Date.UTC(2026,9,18,12)});
  const m=await ready(); await m.setPoints("1500"); m.passwordRef.el={value:"private"}; await m.verifyIdentity();
  assert.ok(m.reauthTimer); const until=m.state.verifiedUntil;
  m.requestClose(); assert.equal(m.reauthTimer,null);
  t.mock.timers.tick(600001); assert.equal(m.state.verifiedUntil,until,"closed component must not receive an expiry mutation");
  const n=await ready(); await n.setPoints("1500"); n.passwordRef.el={value:"private"}; await n.verifyIdentity();
  assert.ok(n.reauthTimer); globalThis.__creditUnmount.at(-1)(); assert.equal(n.reauthTimer,null);
});
test("polling budget begins at delayed first submit and survives same-ID retries",async t=>{
  const opened=Date.UTC(2026,9,18,12);
  t.mock.timers.enable({apis:["setTimeout","Date"],now:opened});
  const m=await ready({convertCredit:async()=>({success:false,code:"llm_network_error"})});
  t.after(()=>m.teardown());
  const id=m.clientRequestId; assert.equal(m.pollStarted,null);
  t.mock.timers.tick(1200000); await m.submit();
  const submitted=opened+1200000;
  assert.equal(m.pollStarted,submitted); assert.equal(m.state.phase,"form");
  t.mock.timers.tick(10000);
  globalThis.__creditApi.convertCredit=async()=>ok({order_id:7,state:"paid"});
  await m.submit();
  assert.equal(m.clientRequestId,id); assert.equal(m.pollStarted,submitted); assert.ok(m.pollTimer,"editing time must not exhaust order polling");
});
