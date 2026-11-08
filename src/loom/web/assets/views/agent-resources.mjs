import {element} from '../markdown.mjs';
import {toolChoices} from './external-tools.mjs';

/** Keep the create form and the Agent's resource editor on the same catalog. */
export function agentResourceChoices(toolList, knowledgeList, catalog, bases, role = {}) {
  const selected = role.external_tool_ids || [], tools = catalog.external_tools || [];
  toolChoices(toolList, [...tools, ...selected.filter(id => !tools.some(t => t.id === id)).map(id => ({
    id, connector_name:'Currently unavailable', name:id, kind:'mcp', description:'Restore the tool configuration or deselect it.',
  }))], selected);
  knowledgeList.replaceChildren();
  const selectedBases = role.knowledge_base_ids || [], available = bases.knowledge_bases || [];
  const choices = [...available, ...selectedBases.filter(id => !available.some(b => b.id === id)).map(id => ({
    id, name:`${id} (currently unavailable)`, description:'Restore the knowledge base or deselect it.',
  }))];
  if (!choices.length) knowledgeList.append(element('p','muted small','No knowledge bases yet; create one under Knowledge bases first.'));
  for (const base of choices) {
    const label = element('label','setup-collection'), check = element('input'), info = element('span');
    check.type = 'checkbox'; check.value = base.id; check.checked = selectedBases.includes(base.id);
    info.append(element('strong','',base.name), element('small','muted',base.description || 'No description'));
    label.append(check,info); knowledgeList.append(label);
  }
}
