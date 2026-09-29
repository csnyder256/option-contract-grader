(() => {
"use strict";
const form=document.getElementById("scenario-form"),output=document.getElementById("scenario-output"),status=document.getElementById("scenario-status");
let last=null,controller=null,epoch=0;
const n=(tag,text,klass)=>{const e=document.createElement(tag);if(text!=null)e.textContent=text;if(klass)e.className=klass;return e;};
const usd=x=>"$"+Number(x).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
function inputs(){
 const obj=Object.fromEntries(new FormData(form).entries());
 for(const k of Object.keys(obj))if(!["label","option_type"].includes(k))obj[k]=obj[k]===""?null:Number(obj[k]);
 obj.iv/=100;obj.iv_change/=100;obj.risk_free_rate/=100;obj.dividend_yield/=100;
 if(obj.historical_vol!=null)obj.historical_vol/=100;
 for(const key of ["bid","ask","historical_vol","iv_rank"])if(obj[key]==null)delete obj[key];
 return obj;
}
function chart(doc){
 const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox","0 0 900 330");svg.setAttribute("role","img");svg.setAttribute("aria-label","Theoretical and expiry profit or loss across underlying prices");
 const rows=doc.rows,xmin=rows[0].spot,xmax=rows.at(-1).spot,values=rows.flatMap(r=>[r.theoretical_pnl_usd,r.expiry_pnl_usd]),lo=Math.min(0,...values),hi=Math.max(0,...values),span=hi-lo||1;
 const x=v=>65+(v-xmin)/(xmax-xmin)*800,y=v=>265-(v-lo)/span*220;
 function elem(tag,attrs,text){const e=document.createElementNS(svg.namespaceURI,tag);for(const [k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text)e.textContent=text;svg.append(e);return e;}
 for(let i=0;i<5;i++){const value=lo+span*i/4,yp=y(value);elem("line",{x1:65,x2:865,y1:yp,y2:yp,stroke:"#33445a","stroke-width":1});elem("text",{x:5,y:yp+4,fill:"#c8d5e6","font-size":12},usd(value));}
 for(const [key,color]of [["expiry_pnl_usd","#8192aa"],["theoretical_pnl_usd","#7bd8c2"]])elem("polyline",{points:rows.map(r=>x(r.spot)+","+y(r[key])).join(" "),fill:"none",stroke:color,"stroke-width":3});
 for(const value of [xmin,doc.inputs.spot,xmax])elem("text",{x:x(value),y:300,fill:"#c8d5e6","font-size":14,"text-anchor":"middle"},usd(value));
 elem("text",{x:450,y:325,fill:"#c8d5e6","font-size":13,"text-anchor":"middle"},"Underlying price · green: theoretical mark · gray: expiry payoff");
 return svg;
}
function paint(doc){
 output.replaceChildren();const s=doc.summary;
 const stats=n("div",null,"scenario-stats");
 [["Maximum long-position loss",usd(s.max_loss_usd)],["Expiry break-even",s.expiry_break_even==null?"Unreachable":usd(s.expiry_break_even)],["Days remaining",String(s.remaining_days)],["Scenario IV",(s.scenario_iv*100).toFixed(1)+"%"]].forEach(([label,value])=>{const tile=n("div",null,"scenario-stat");tile.append(n("span",label),n("strong",value));stats.append(tile);});
 output.append(stats,chart(doc));
 const grade=renderCard(doc.grade);output.append(grade);
 const assumptions=n("details"),list=n("ul");doc.assumptions.forEach(a=>list.append(n("li",a)));assumptions.append(n("summary","Model assumptions and limitations"),list);
 const provenance=n("details");provenance.append(n("summary","Reproduce this report"),n("pre",JSON.stringify({inputs:doc.inputs,provenance:doc.provenance},null,2)));
 const values=n("details"),table=n("table",null,"detail-table"),head=n("tr");["Spot","Theoretical P/L","Expiry P/L"].forEach(t=>head.append(n("th",t)));table.append(head);
 doc.rows.forEach(r=>{const row=n("tr");[r.spot,r.theoretical_pnl_usd,r.expiry_pnl_usd].forEach(v=>row.append(n("td",usd(v))));table.append(row);});values.append(n("summary","Inspect all scenario values"),table);
 output.append(assumptions,provenance,values);
 document.querySelectorAll("[data-scenario-export]").forEach(b=>b.disabled=false);
}
async function run(){
 if(!form.reportValidity())return;
 const owner=++epoch;if(controller)controller.abort();controller=new AbortController();
 const body=inputs();last=null;document.querySelectorAll("[data-scenario-export]").forEach(b=>b.disabled=true);output.replaceChildren();status.textContent="Calculating local model scenarios…";
 try{
  const response=await fetch("/scenario",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),signal:controller.signal}),doc=await response.json();
  if(owner!==epoch)return;if(!response.ok)throw Error(typeof doc.detail==="string"?doc.detail:JSON.stringify(doc.detail));
  last={inputs:doc.inputs,report:doc};paint(doc);status.textContent="Hypothetical scenarios calculated. No data provider or trading API was called.";
 }catch(err){if(owner===epoch&&err.name!=="AbortError")status.textContent="Scenario failed: "+err.message;}
}
form.addEventListener("submit",event=>{event.preventDefault();run();});
document.getElementById("scenario-example").addEventListener("click",()=>{
 form.reset();form.elements.label.value="Illustrative 30-day call";document.querySelector('[data-tab="scenario"]').click();run();
});
document.querySelectorAll("[data-scenario-export]").forEach(button=>button.addEventListener("click",async()=>{
 if(!last)return;const format=button.dataset.scenarioExport,snapshot=last;
 try{
  const response=await fetch("/scenario/report?format="+format,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(snapshot.inputs)});
  if(!response.ok)throw Error("Export failed: HTTP "+response.status);
  const url=URL.createObjectURL(await response.blob()),a=n("a");a.href=url;a.download="option-scenario."+format;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 }catch(err){status.textContent=err.message;}
}));
window.optionScenario={
 open(contract){
  ++epoch;if(controller)controller.abort();last=null;output.replaceChildren();document.querySelectorAll("[data-scenario-export]").forEach(b=>b.disabled=true);
  const ctx=contract.scenario_context||{};
  const values={label:contract.underlying+" "+contract.strike+" "+contract.type,option_type:contract.type,spot:ctx.spot||contract.strike,strike:contract.strike,premium:contract.premium_per_share,dte:contract.dte,elapsed_days:Math.min(7,contract.dte),iv:(contract.iv??.25)*100,risk_free_rate:(ctx.risk_free_rate??.04)*100,dividend_yield:(ctx.dividend_yield??0)*100,bid:contract.bid,ask:contract.ask,volume:contract.volume,open_interest:contract.open_interest,historical_vol:ctx.historical_vol==null?"":ctx.historical_vol*100,iv_rank:ctx.iv_rank??""};
  form.reset();for(const [key,value]of Object.entries(values))if(form.elements[key])form.elements[key].value=value;
  document.querySelector('[data-tab="scenario"]').click();status.textContent=ctx.spot?"Quote inputs loaded. Review the assumed purchase premium and simulate.":"Underlying spot was not recorded; enter it before simulating.";form.elements.spot.focus();
 }
};
})();
