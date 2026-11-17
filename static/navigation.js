'use strict';
(()=>{
  const nav=document.getElementById('workspace-navigation');
  const toggle=document.getElementById('navigation-toggle');
  const links=[...nav.querySelectorAll('a[href^="#"]')];
  const targets=links.map(link=>document.getElementById(link.hash.slice(1)));
  document.body.dataset.navReady='true';
  function menu(open){toggle.setAttribute('aria-expanded',String(open));nav.classList.toggle('navigation-open',open);}
  toggle.addEventListener('click',()=>menu(toggle.getAttribute('aria-expanded')!=='true'));
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&toggle.getAttribute('aria-expanded')==='true'){menu(false);toggle.focus();}});
  function mark(id){
    for(const link of links){const current=link.hash==='#'+id;link.classList.toggle('nav-active',current);if(current)link.setAttribute('aria-current','location');else link.removeAttribute('aria-current');}
  }
  let queued=false;
  function update(){
    queued=false;let id='workspace';
    for(const target of targets){if(target.id!=='workspace'&&target.getBoundingClientRect().top<=110)id=target.id;}
    if(window.scrollY+window.innerHeight>=document.documentElement.scrollHeight-2)id=targets.at(-1).id;
    mark(id);
  }
  function schedule(){if(!queued){queued=true;requestAnimationFrame(update);}}
  function focusDestination(hash){
    const target=document.getElementById(hash.slice(1));if(!target)return;
    const heading=target.querySelector('h1,h2')||target;heading.setAttribute('tabindex','-1');heading.focus({preventScroll:true});
    mark(target.id);
  }
  document.addEventListener('click',event=>{
    const link=event.target.closest('a[href^="#"]');
    if(!link||event.defaultPrevented||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey||event.button!==0)return;
    menu(false);focusDestination(link.hash);
  });
  window.addEventListener('hashchange',()=>{focusDestination(location.hash);schedule();});
  window.addEventListener('scroll',schedule,{passive:true});window.addEventListener('resize',schedule);
  const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
  if(reducedMotion.matches)document.documentElement.style.scrollBehavior='auto';
  // Loaded studios retain independent snapshots. This is the saved run selected in history.
  function selected(run){
    document.getElementById('navigation-run').textContent=run?run.label+' · '+run.id.slice(0,8)+' · '+run.status:'None selected. Assess sources to create one, or open a saved run.';
  }
  window.addEventListener('crosslake:run-selected',event=>selected(event.detail));
  selected(window.monitor?.selectedRun());
  if(location.hash)focusDestination(location.hash);schedule();
})();
