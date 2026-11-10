const $=(s,root=document)=>root.querySelector(s);
const $$=(s,root=document)=>[...root.querySelectorAll(s)];
const data=await fetch('/data.json').then(r=>r.json());
const runtime=()=>fetch('/api/runtime').then(r=>r.json());
const action=(name,fields={})=>fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,...fields})}).then(r=>r.json());
const tick=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
const html=(value)=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const show=(s)=>{$('#status').textContent=s;};

const loadJobs=()=>JSON.parse(localStorage.getItem('jobs')||'[]');
function filter(){return `${$('#period').value}|${$('#vendor').value}|${$('#currency').value}`;}
function summary(){$('#filter').textContent=filter();}
function render(){const jobs=loadJobs();$('#jobs').innerHTML=jobs.map(j=>`<article class="panel" data-job="${j.id}" data-owner="${j.owner}" data-filter="${j.filter}"><p>${j.id} ${j.owner} ${j.filter}</p><p class="job-state">${j.state}</p><button data-ready="${j.id}">刷新任务</button><button data-download="${j.id}" ${j.state==='完成'?'':'disabled'}>下载结算资料.csv</button></article>`).join('');for(const b of $$('[data-ready]'))b.onclick=()=>{const all=loadJobs();all.find(j=>j.id===b.dataset.ready).state='完成';localStorage.setItem('jobs',JSON.stringify(all));render();};for(const b of $$('[data-download]'))b.onclick=async()=>{const job=loadJobs().find(j=>j.id===b.dataset.download);const state=await runtime();const meta={job_id:state.phase==='wrong-file'?'J-OLD':job.id,filter_digest:job.filter,schema_version:1};const blob=new Blob([JSON.stringify(meta)+'\n资料编号,金额\nX17,73.25'],{type:'text/csv'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='结算资料.csv';a.click();await action('download',{job_id:job.id});};}
if(!localStorage.getItem('jobs'))localStorage.setItem('jobs',JSON.stringify([{id:'J-OLD',owner:'other-user',filter:'2026-09|north|CNY',state:'完成'}]));
$('#generate').onclick=async()=>{const jobs=loadJobs();const id='J-'+String(jobs.length+1);jobs.push({id,owner:'demo-user',filter:filter(),state:'排队'});localStorage.setItem('jobs',JSON.stringify(jobs));$('#created').textContent=id;if((await runtime()).phase==='popup-on-generate')window.open('/?center=1','exports');await action('export',{job_id:id,filter:filter()});render();};
$('#center').onclick=()=>window.open('/?center=1','exports');
for(const control of $$('input,select'))control.onchange=summary;
window.addEventListener('storage',render);summary();render();show('导出中心可用，任务进度由来源逐步推进');
