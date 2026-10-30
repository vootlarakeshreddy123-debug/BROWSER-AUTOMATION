import asyncio, json
from playwright.async_api import async_playwright
from agent.planner import try_live_path
from browser.executor import execute_plan
from browser.inspector import snapshot
from agent.recovery import RetryBudget

TOPICS=[
("machine learning","Machine Learning","Machine learning is a field of artificial intelligence that enables systems to learn from data.","Relationship to artificial intelligence","Machine learning is a subset of artificial intelligence focused on learning from data."),
("artificial intelligence","Artificial Intelligence","Artificial intelligence is the study of systems that perform tasks associated with human intelligence.","Relationship to machine learning","Machine learning is one approach used to build artificial intelligence systems."),
("python","Python","Python is a high-level programming language known for readable syntax.","Use in software development","Python is widely used for scripting, web development, automation, and data work."),
("databases","Databases","A database is an organized collection of data that can be queried and updated.","Relational databases","Relational databases organize information into tables connected by defined relationships."),
("cloud computing","Cloud Computing","Cloud computing provides computing resources over a network on demand.","Service models","Cloud service models commonly include infrastructure, platform, and software services."),]
TASKS=[]
for q,title,first,section,secfirst in TOPICS:
 TASKS += [
  f"Search for {q}, open the first article, and tell me the title of the page.",
  f"Search for {q}, open the first article, and tell me the first heading on the page.",
  f"Search for {q}, open the first article, and tell me the first sentence of the section about {section.lower()}.",
  f"Search for {q}, open the first article, find the section about {section.lower()}, and tell me its first sentence.",
  f"Search for {q}, open the first article, and extract the first sentence from the section about {section.lower()}.",
 ]
HTML='''<!doctype html><html><head><title>Search</title></head><body><main id="app"></main><script>const data=%s;const shell=`<h1>Search Results</h1><input id="search" type="search" placeholder="Search" aria-label="Search"><button id="go">Search</button><div id="results"></div>`;function bind(){const input=document.querySelector('#search'),results=document.querySelector('#results');function render(){document.title='Search Results';results.innerHTML=data.map((d,i)=>`<a href="/article/${i}" data-i="${i}" class="result">${d[1]}</a>`).join('')}function openArticle(i){const d=data[i];document.title=d[1];document.querySelector('#app').innerHTML=`<article><h1>${d[1]}</h1><p>${d[2]}</p><h2>${d[3]}</h2><p>${d[4]}</p><p>Additional neutral background information about ${d[1]}.</p></article>`}input.addEventListener('keydown',e=>{if(e.key==='Enter')render()});document.querySelector('#go').onclick=render;results.addEventListener('click',e=>{const a=e.target.closest('a.result');if(!a)return;e.preventDefault();openArticle(+a.dataset.i)})}window.resetFixture=()=>{document.title='Search';document.querySelector('#app').innerHTML=shell;bind()};window.resetFixture()</script></body></html>'''%json.dumps(TOPICS)

async def run_one(page,task):
 await page.evaluate('window.resetFixture()')
 completed=[]; budget=RetryBudget(2)
 for _ in range(10):
  ctx=await snapshot(page);ctx['current_url']=page.url;ctx['current_title']=await page.title();ctx['completed_actions']=completed
  plan=try_live_path(task,ctx)
  if plan is None: raise AssertionError(f'NO_PLAN url={page.url} title={await page.title()} actions={completed}')
  if plan.done:
   if not any(x['action'].get('type')=='click' for x in completed): raise AssertionError('FALSE_DONE')
   return
  result=await execute_plan(page,plan.actions,budget)
  for a in plan.actions: completed.append({'action':a.model_dump(mode='json'),'result':result})
  if result.get('extracted'):
   if not str(result['extracted'][-1].get('text') or '').strip(): raise AssertionError('EMPTY_EXTRACT')
   return
 raise AssertionError('STEP_LIMIT')

async def main():
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
  page=await browser.new_page(); await page.set_content(HTML)
  passed=0
  for i,task in enumerate(TASKS,1):
   await run_one(page,task);passed+=1;print(f'{i:02d} PASS')
  await browser.close();print(f'PASS_COUNT={passed}/{len(TASKS)}')
if __name__=='__main__':asyncio.run(main())
