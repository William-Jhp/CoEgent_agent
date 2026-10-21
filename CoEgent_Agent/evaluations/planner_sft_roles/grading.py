"""Bounded symbolic grading using canonical state effects; no backend/skill runs."""
from copy import deepcopy
from coegent_contracts.state import StateContract
from coegent_contracts.semantics import apply_effects,valid_success_data

def traces(plan,case,binding):
    state=deepcopy(case['state']);actions=[]
    def run(nodes):
        nonlocal state
        for node in nodes:
            kind=node['type']
            if kind=='sequence':run(node['nodes'])
            elif kind=='condition':
                result=StateContract.condition(state,node['condition'])
                run(node['then'] if result else node['else'])
            elif kind=='skill':
                original=node['name'];name,args=original,deepcopy(node['args'])
                if name=='navigate_to' and args.get('target')==state['robot']['home']:
                    name,args='return_home',{}
                actions.append({'name':name,'args':args})
                data={};robot,world=state['robot'],state['world']
                if name=='get_battery':data={'battery_percent':binding.get('battery_percent',robot['battery_percent'])}
                elif name=='get_pose':data={'location':robot['location']}
                elif name=='inspect_target':
                    target=args['target'];known=world.get('targets',{}).get(target,{})
                    data={'target':target,'location':robot['location'],
                          'target_exists':binding.get('target_exists',known.get('exists',False)),
                          'valve_state':binding.get('valve_state',known.get('valve_state','unknown'))}
                elif name=='check_path':data={'target':args['target'],'path_clear':world.get('paths',{}).get(args['target'])=='clear'}
                elif name in ('navigate_to','return_home'):data={'location':args['target'] if name=='navigate_to' else robot['home']}
                elif name in ('open_valve','close_valve'):
                    data={'target':args['target'],'valve_state':'open' if name=='open_valve' else 'closed'}
                elif name=='rotate':data={'angle_deg':args['angle_deg']}
                elif name=='stop':data={'stopped':True}
                elif name=='log_event':data={'logged':True}
                else:raise ValueError('unsupported_symbolic_skill:'+name)
                # Preserve original skill name/aliases even when equivalent HOME moves normalize for comparison.
                if not valid_success_data(original,node['args'],state,data):raise ValueError('invalid_fixture_result:'+original)
                state=apply_effects(state,original,node['args'],data,node.get('save_as'))
            else:raise ValueError('unsupported_control:'+kind)
    run(plan['sequence'])
    return actions

def allowed_logging(actual,expected):
    index=0
    for action in actual:
        if index<len(expected) and action==expected[index]:index+=1
        elif action['name']!='log_event':return False
    return index==len(expected)

def operational(actual,expected):
    """Exploratory only: allow logs and check_path for the immediately following requested move."""
    kept=[]
    for i,action in enumerate(actual):
        if action['name']=='log_event':
            kept.append(action);continue
        if action['name']=='check_path':
            later=next((x for x in actual[i+1:] if x['name'] not in ('check_path','log_event')),None)
            target=(later['args'].get('target') if later and later['name']=='navigate_to' else 'HOME' if later and later['name']=='return_home' else None)
            if action['args'].get('target')==target:continue
        kept.append(action)
    return allowed_logging(kept,expected)

def score(plan,case):
    strict=relaxed=ops=True
    if plan.get('final_outcome')!=case['reference'].get('final_outcome'):
        return {'strict':False,'relaxed':False,'operational':False,'reason':'final_outcome_mismatch'}
    try:
        for binding in case['bindings']:
            expected=traces(case['reference'],case,binding)
            actual=traces(plan,case,binding)
            strict=strict and actual==expected
            relaxed=relaxed and allowed_logging(actual,expected)
            ops=ops and operational(actual,expected)
    except (ValueError,TypeError,KeyError) as exc:
        return {'strict':False,'relaxed':False,'operational':False,'reason':str(exc)}
    return {'strict':bool(strict),'relaxed':bool(relaxed),'operational':bool(ops),'reason':None if strict else 'trace_mismatch'}
