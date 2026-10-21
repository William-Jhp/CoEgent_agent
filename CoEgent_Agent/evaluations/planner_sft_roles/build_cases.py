import json
from pathlib import Path
from copy import deepcopy
from coegent.scenarios import initial_state,skill

cases=[]
def add(command,nodes,*,location='HOME',bindings=None):
    state=initial_state().snapshot();state['robot']['location']=location
    cases.append({'id':f'p{len(cases)+1:02}','command':command,'state':state,
        'reference':{'schema_version':'planner.v1','goal':command,'sequence':nodes,
                     'success_criteria':['요청한 작업 관측 확인'],'final_outcome':'execute'},
        'bindings':bindings or [{}]})
def cond(source,op,value,yes,no):
    return {'type':'condition','condition':{'source':source,'operator':op,'value':value},'then':yes,'else':no}
B=skill('get_battery');P=skill('get_pose');A=skill('navigate_to',target='A');G=skill('navigate_to',target='B');H=skill('return_home')
I=skill('inspect_target',target='red_valve');C=skill('close_valve',target='red_valve');O=skill('open_valve',target='red_valve')
add('현재 배터리 잔량만 확인해.',[B])
add('현재 위치만 확인해.',[P])
add('A로 이동해. 도착하면 작업을 끝내.',[A])
add('B로 이동해. 추가 작업은 하지 마.',[G])
add('HOME으로 복귀해.',[H],location='A')
add('90도 회전해.',[skill('rotate',angle_deg=90)])
add('-45도 회전해.',[skill('rotate',angle_deg=-45)])
add('배터리를 확인한 다음 현재 위치를 확인해. 이 순서를 지켜.',[B,P])
add('현재 위치부터 확인하고 그다음 배터리를 확인해.',[P,B])
add('A로 이동한 뒤 HOME으로 복귀해.',[A,H])
add('B에 갔다가 HOME으로 돌아와.',[G,H])
add('A, B, HOME 순서로 이동해.',[A,G,H])
add('현재 장소의 red_valve를 점검만 해. 밸브를 조작하지 마.',[I],location='A')
add('red_valve를 점검한 다음 닫아.',[I,C],location='A')
add('red_valve를 점검한 다음 열어.',[I,O],location='A')
add('배터리는 확인하지 말고 현재 위치만 확인해.',[P])
add('30도 회전한 다음 HOME으로 복귀해.',[skill('rotate',angle_deg=30),H],location='A')
add('현재 위치를 확인하고 마지막에 정지해.',[P,skill('stop')])
valves=[{'valve_state':'open'},{'valve_state':'closed'}]
add('red_valve를 점검한 뒤 열려 있으면 닫고, 닫혀 있으면 "already_closed"를 로그에 기록해.',
    [I,cond('inspection.valve_state','==','open',[C],[skill('log_event',message='already_closed')])],location='A',bindings=valves)
add('red_valve를 점검한 뒤 닫혀 있으면 열고, 열려 있으면 "already_open"을 로그에 기록해.',
    [I,cond('inspection.valve_state','==','closed',[O],[skill('log_event',message='already_open')])],location='A',bindings=valves)
add('배터리를 새로 읽고, 읽은 잔량이 20 미만이면 HOME으로 복귀하고 그렇지 않으면 현재 위치를 확인해.',
    [B,cond('get_battery.battery_percent','<',20,[H],[P])],location='A',
    bindings=[{'battery_percent':x} for x in (5,19,20,21,80)])
add('배터리를 새로 읽고, 잔량이 90 이상이면 90도 회전하고 그렇지 않으면 현재 위치를 확인해.',
    [B,cond('get_battery.battery_percent','>=',90,[skill('rotate',angle_deg=90)],[P])],
    bindings=[{'battery_percent':x} for x in (5,89,90,100)])
add('red_valve를 점검하고 대상이 존재하면 현재 위치를 확인하고, 존재하지 않으면 정지해.',
    [I,cond('inspection.target_exists','==',True,[P],[skill('stop')])],location='A',
    bindings=[{'target_exists':True},{'target_exists':False}])
add('A로 이동해서 red_valve를 점검해. 열려 있으면 닫고, 아니면 "unchanged"를 로그에 기록한 다음 HOME으로 복귀해.',
    [A,I,cond('inspection.valve_state','==','open',[C],[skill('log_event',message='unchanged')]),H],bindings=valves)
Path(__file__).with_name('planner_cases.json').write_text(json.dumps({'version':2,'cases':cases},ensure_ascii=False,indent=2)+'\n')
print('Frozen planner cases',len(cases))
