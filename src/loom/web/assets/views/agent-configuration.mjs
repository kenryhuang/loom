import {element} from '../markdown.mjs';
import {field, selectField, preset} from './agent-setup.mjs';
import {agentResourceChoices} from './agent-resources.mjs';
import {selectedTools} from './external-tools.mjs';

export const configurationTitles = {identity:'角色与方法', models:'模型', execution:'执行配置',
  scheduling:'运行与调度', budget:'每日预算', learning:'学习与评估', resources:'资料与资源'};
export const configuredRole = detail => detail.configuration?.role || detail.role;
const lines = value => value.split('\n').map(row=>row.trim()).filter(Boolean);
const intervals = [['60','1 分钟'],['300','5 分钟'],['900','15 分钟'],['3600','1 小时'],['86400','每天']];
const budgets = [['250000','25 万 token'],['1000000','100 万 token'],['10000000','1000 万 token'],['100000000','1 亿 token']];
const action = (label, callback) => {
  const button=element('button','quiet',label);button.type='button';button.onclick=callback;return button;
};
function choices(select, rows, configured) {
  select.replaceChildren();
  for(const [value,label] of [['','服务默认'],...rows]) {
    const option=element('option','',label);option.value=value;select.append(option);
  }
  if(configured && ![...select.options].some(option=>option.value===configured)) {
    const missing=element('option','',`${configured}（当前不可用）`);missing.value=configured;select.append(missing);
  }
  select.value=configured || '';
}

export class AgentConfigurationEditor {
  constructor(root,{detail,section,api,save,cancel}) {
    this.root=root;this.detail=detail;this.section=section;this.api=api;this.save=save;this.cancel=cancel;
    this.role=structuredClone(configuredRole(detail));this.abort=new AbortController();this.closed=false;
    const form=this.form=element('form','agent-form agent-configuration-form');form.dataset.configurationEditor=section;
    form.setAttribute('aria-label',`编辑${configurationTitles[section]}`);
    form.append(element('h4','',`编辑${configurationTitles[section]}`),
      element('p','muted small','保存后用于下一次新任务调度；已派发任务及其恢复继续使用原配置。'));
    this.fields=element('div','setup-settings-grid');form.append(this.fields);
    this.build();
    this.error=element('p','error');this.error.setAttribute('role','alert');
    this.retry=action('重新加载选项',()=>this.load());this.retry.hidden=true;
    this.submit=element('button','primary','保存参数');this.submit.type='submit';
    this.cancelButton=action('取消',()=>{this.close();this.cancel();});
    const actions=element('div','setup-form-actions');actions.append(this.submit,this.cancelButton);
    form.append(this.error,this.retry,actions);root.append(form);
    form.onsubmit=async event=>{
      event.preventDefault();if(this.submit.disabled || !form.reportValidity())return;
      this.submit.disabled=true;this.cancelButton.disabled=true;this.error.textContent='';
      try{await this.save(this.changes());}
      catch(error){if(!this.closed)this.error.textContent=`${error.message}${error.status===409?'。请取消后重新打开，核对最新配置。':''}`;}
      finally{if(!this.closed){this.submit.disabled=false;this.cancelButton.disabled=false;}}
    };
  }
  close(){this.closed=true;this.abort.abort();this.form.remove();}
  build(){
    const p=this.fields,r=this.role;
    if(this.section==='identity') {
      field(p,'name','名称',r.name,{required:true});field(p,'role_text','角色说明',r.role_text,{required:true,multiline:true});
      field(p,'methods','默认方法（每行一条）',(r.methods || []).join('\n'),{multiline:true});
    } else if(this.section==='models') {
      this.model=selectField(p,'model','执行与规划模型',[['','加载模型…']]);
      this.evaluation=selectField(p,'evaluation_model','评估模型',[['','加载模型…']]);
    } else if(this.section==='budget') {
      this.daily=preset(p,'daily_tokens','每日 token 上限',budgets,r.budget?.daily_tokens || 1000000);
      p.append(element('p','muted small',`规划、执行、验收和学习共享每日额度，按 ${r.timezone || 'UTC'} 零点重新计数。`));
    } else if(this.section==='scheduling') {
      const s=r.scheduling_policy || {};
      this.interval=preset(p,'interval_seconds','目标检查间隔（秒）',intervals,r.interval_seconds || 86400,{min:10,max:31536000});
      this.enabled=selectField(p,'enabled','定时调度',[['true','开启'],['false','仅手动调度']],String(s.enabled ?? true));
      this.tick=preset(p,'tick_seconds','调度检查间隔（秒）',intervals,s.tick_seconds || 60,{min:1,max:31536000});
      this.recovery=preset(p,'max_recovery_attempts','每任务自动恢复次数',[['0','0 次'],['3','3 次'],['5','5 次']],s.max_recovery_attempts ?? 5,{min:0,max:10});
      let zones=Intl.supportedValuesOf ? Intl.supportedValuesOf('timeZone') : [];
      zones=[...new Set([r.timezone || 'UTC','UTC',...zones])];
      this.zone=selectField(p,'timezone','时区',zones.map(zone=>[zone,zone]),r.timezone || 'UTC');
    } else if(this.section==='learning') {
      const l=r.learning_policy || {};
      this.resumes=preset(p,'max_resumes','每个评估每日自动续跑次数',[['0','0 次'],['3','3 次'],['5','5 次']],l.max_resumes ?? 3,{min:0,max:10});
      this.attempts=preset(p,'max_attempts','每轮分析尝试次数',[['3','3 次'],['6','6 次'],['10','10 次']],l.max_attempts ?? 6,{min:1,max:20});
      this.calls=preset(p,'max_evaluation_calls','单个评估调用次数上限',[['100','100 次'],['250','250 次'],['500','500 次']],l.max_evaluation_calls ?? 500,{min:1,max:500});
      this.methodLearning=selectField(p,'method_learning','自动方法学习',[['false','关闭'],['true','开启']],String(r.method_learning ?? false));
    } else if(this.section==='execution') {
      this.plan=selectField(p,'plan_mode','规划模式',[['auto','自动'],['force','强制规划'],['off','直接执行']],r.plan_mode || 'auto');
      this.context=selectField(p,'context_plugin','上下文插件',[['','服务默认']]);
      this.workflow=selectField(p,'workflow_plugin','工作流插件',[['','服务默认']]);
      this.collections=element('div','external-choices');this.collections.setAttribute('aria-label','工具集合');p.append(this.collections);
    } else if(this.section==='resources') {
      field(p,'paths','资料路径（每行一条）',(r.source_routes?.[0]?.paths || []).join('\n'),{multiline:true});
      this.knowledge=element('div','knowledge-choices');this.tools=element('div','external-choices');
      this.knowledge.setAttribute('aria-label','知识库');this.tools.setAttribute('aria-label','外部工具');p.append(this.knowledge,this.tools);
    }
  }
  async load(){
    if(!['models','execution','resources'].includes(this.section))return;
    this.submit.disabled=true;this.retry.hidden=true;this.error.textContent='';
    try{
      const catalog=await this.api.json('/v1/web/catalog',{signal:this.abort.signal});if(this.closed)return;
      const r=this.role;
      if(this.section==='models') {
        const models=(catalog.models || []).map(model=>[model.id,model.label || model.id]);
        choices(this.model,models,catalog.model_aliases?.[r.model] || r.model);
        choices(this.evaluation,models,catalog.model_aliases?.[r.evaluation_model] || r.evaluation_model);
      } else if(this.section==='execution') {
        choices(this.context,(catalog.plugins?.context || []).map(plugin=>[plugin.id,plugin.label || plugin.id]),r.task_spec?.context?.plugin);
        choices(this.workflow,(catalog.plugins?.workflow || []).map(plugin=>[plugin.id,plugin.label || plugin.id]),r.task_spec?.workflow?.plugin);
        this.collections.replaceChildren();
        const selected=new Set(r.task_spec?.tools?.collections || []),available=new Set();
        for(const row of catalog.tool_collections || []) {
          if(['knowledge','external_tools'].includes(row.id))continue;available.add(row.id);
          const label=element('label'),check=element('input');check.type='checkbox';check.value=row.id;check.checked=selected.has(row.id);
          label.append(check,document.createTextNode(row.label || row.id));this.collections.append(label);
        }
        for(const id of selected)if(!available.has(id) && !['knowledge','external_tools'].includes(id)) {
          const label=element('label'),check=element('input');check.type='checkbox';check.value=id;check.checked=true;
          label.append(check,document.createTextNode(`${id}（当前不可用）`));this.collections.append(label);
        }
      } else {
        const bases=await this.api.json('/v1/knowledge-bases',{signal:this.abort.signal});if(this.closed)return;
        agentResourceChoices(this.tools,this.knowledge,catalog,bases,r);
      }
      this.submit.disabled=false;
    }catch(error){if(!this.closed && error.name!=='AbortError'){this.error.textContent=error.message;this.retry.hidden=false;}}
  }
  changes(){
    const f=this.form.elements,r=this.role;
    if(this.section==='identity')return {name:f.name.value.trim(),role_text:f.role_text.value.trim(),methods:lines(f.methods.value)};
    if(this.section==='models')return {model:this.model.value || null,evaluation_model:this.evaluation.value || null};
    if(this.section==='budget')return {budget:{daily_tokens:this.daily.value()}};
    if(this.section==='scheduling')return {interval_seconds:this.interval.value(),timezone:this.zone.value,
      scheduling_policy:{enabled:this.enabled.value==='true',tick_seconds:this.tick.value(),max_recovery_attempts:this.recovery.value()}};
    if(this.section==='learning')return {method_learning:this.methodLearning.value==='true',learning_policy:{
      max_resumes:this.resumes.value(),max_attempts:this.attempts.value(),max_evaluation_calls:this.calls.value()}};
    if(this.section==='resources') {
      const changes={source_routes:structuredClone(r.source_routes || []),knowledge_base_ids:selectedTools(this.knowledge),external_tool_ids:selectedTools(this.tools)};
      const paths=lines(f.paths.value);
      if(changes.source_routes.length)changes.source_routes[0].paths=paths;
      else if(paths.length)changes.source_routes.push({id:'implementation',topic:'实现资料',paths});
      for(const route of changes.source_routes)for(const key of ['knowledge_base_ids','external_tool_ids'])
        if(route[key])route[key]=route[key].filter(id=>changes[key].includes(id));
      return changes;
    }
    const spec=structuredClone(r.task_spec || {});spec.schema_version=1;
    const collections=selectedTools(this.collections);
    if(collections.length)(spec.tools ||= {}).collections=collections;
    else if(spec.tools)delete spec.tools.collections;
    for(const [kind,plugin] of [['context',this.context.value],['workflow',this.workflow.value]]) {
      if(plugin)spec[kind]={...(spec[kind] || {}),plugin};else delete spec[kind];
    }
    if(spec.tools && !Object.keys(spec.tools).length)delete spec.tools;
    return {plan_mode:this.plan.value,task_spec:Object.keys(spec).some(key=>key!=='schema_version')?spec:null};
  }
}
