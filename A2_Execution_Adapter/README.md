# A2 Execution Adapter

CoEgent의 `a2-isaac` 계획을 기존 `a2-llm-isaac` Python API로 실행하는 별도 패키지입니다. 기본값은 **이동 비허용**입니다. 실행은 터미널에 붙어 있고, 새 시뮬레이터나 상시 에이전트 서버를 띄우지 않습니다.

```text
자연어 → CoEgent Selector / Planner (기본 E4B GGUF + llama.cpp)
       → planner.v1 검증 → a2-isaac ExecutorAdapter
       → 전용 Docker worker의 MissionEngine + A2 API router
       → 기존 RobotBackend / GridNavigator / WorldState → ROS2 → office_sim
```

CoEgent의 기존 MissionEngine을 재사용합니다. 새 인터프리터를 복제하지 않았습니다. ROS와 영상 객체는 작업 프로세스 안에 있고, 모델 프로세스에는 ROS 라이브러리를 import하지 않습니다. 모델은 계획 후 해제하고 ROS 실행을 시작합니다.

선택적 Inspector 설정 시에는 worker가 실제 상태·카메라 관측으로 완료를 검증하고, `run`은 정지 확인 후 제한된 횟수의 재계획을 수행합니다. 이 경우 in-process planner는 재계획을 위해 미션 종료까지 유지합니다. `request`는 플래너 없이 점검 결과를 반환합니다. [Inspector 설정과 한계](../CoEgent_Agent/docs/INSPECTOR.md).

## 설치

현재 개발 환경에는 아래 설치와 이미지 빌드를 수행했습니다. 재설치할 때만 실행합니다.

```bash
cd /home/gritandgrind/dev/pjh/local_ai_study
bash A2_Execution_Adapter/scripts/setup.sh
./.venv/bin/python -m coegent executors
# a2-isaac, coegent
```

기존 `a2-llm-planner:sim` 이미지를 기반으로 `coegent-a2-adapter:sim`을 별도로 만듭니다. 원본 이미지/서비스는 변경하지 않습니다. 모델 파일은 복사하지 않으며 기존 `LLM_Model_Server/model/gguf/gemma-4-E4B_q4_0-it.gguf`를 사용합니다.

## 터미널에서 실행

기본 `run_a2_isaac.sh` 및 `CoEgent_Agent/scripts/plan_a2_isaac.sh`도 cuVSLAM360 scene `9230e3f6…`를 선택하도록 설정했습니다. 실행 기본값은 읽기 전용입니다. 이 배포 설정은 현재 Isaac office용이며 아래 Dangjin workbench와의 연결을 뜻하지 않습니다.

사용자가 켜 둔 Isaac GUI와 ROS 서비스는 그대로 둡니다.

```bash
cd /home/gritandgrind/dev/pjh/local_ai_study

# 실제 ROS 상태 조회. 이동/localization/lifecycle 명령 없음.
./.venv/bin/python -m a2_execution_adapter diagnose

# E4B Selector → Planner → 실제 배터리 조회 API
bash A2_Execution_Adapter/scripts/run_a2_isaac.sh \
  --command '현재 배터리 잔량만 확인해.'

# 영상 획득 API. 영상 분석 서버를 호출하지 않는 명령.
bash A2_Execution_Adapter/scripts/run_a2_isaac.sh \
  --command '전방 카메라 영상만 가져와. 분석이나 이동은 하지 마.'
```

기본 결과는 `A2_Execution_Adapter/results/run.json`에 저장합니다. `--out`으로 분리할 수 있습니다. 실제 API 시작/결과와 localization/path 진행 상황이 터미널에 출력됩니다. `Ctrl+C`는 모델 계산 또는 이 Adapter가 소유한 작업을 취소합니다. 중단 확인이 실패하면 `SAFETY_ABORT`와 `stop_confirmation.confirmed=false`를 반환합니다. 컨테이너 종료 자체를 로봇 정지 확인으로 취급하지 않습니다.

## 이동 허용 설정

### 현재 office_sim의 cuVSLAM 지도 선택

현재 localization 서비스는 `a2_sim_cuvslam360.yaml`을 사용합니다. 이에 맞춰 별도 scene bundle과 설정을 추가했습니다. 기존 LiDAR 설정 대신 다음 launcher를 사용합니다.

```bash
# 읽기 전용: E4B가 선택된 cuVSLAM 지도 문맥으로 계획하고 배터리를 조회
bash A2_Execution_Adapter/scripts/run_a2_cuvslam.sh \
  --command '현재 배터리 잔량만 확인해.'

# 이동 허용: 현재 열려 있는 Isaac Sim에 연결
bash A2_Execution_Adapter/scripts/run_a2_cuvslam.sh \
  --executor-options A2_Execution_Adapter/config/cuvslam360_simulation.json \
  --command 'short test point로 이동해.'

# 선택된 지도만 계획 모드에 사용
bash CoEgent_Agent/scripts/plan_a2_isaac.sh \
  --map-scene A2_Execution_Adapter/maps/cuvslam360_9230e3f6 \
  --command 'short test point로 이동해.'
```

선택 지도는 **`9230e3f6-084f-4330-a654-945fb5ab398b`**, camera profile은 **cuVSLAM360**입니다. native MDB/PCD/metadata는 KTS 원본 위치에서 읽으며 모델처럼 복사하지 않았습니다. 별도 `maps/cuvslam360_9230e3f6`에는 이 지도에서 생성한 경로 격자·메타데이터·미리보기만 있습니다. 일반 생성기의 천장 혼입을 피하려고 이 평면 Isaac 장면에서는 native 깊이 점군의 바닥 후보(-0.7~-0.3m)를 region 성장 전에 제한했고, 장애물/머리 위 공간 검사는 전체 점군을 사용했습니다. 원본 지도나 ROS TF는 변경하지 않았습니다.

`short test point`는 새 지도 좌표 **(1.2,-1.6)**이며 상대 이동량이 아닙니다. Office 왕복 시험용으로 기존 저장 장소 `start point` **(0.371,-0.762)**와 `west open area` **(-14.458,20.651)**도 등록했습니다. 이전 native cuVSLAM 주행과 독립 시뮬레이터 평가 궤적의 고정 좌표 정합으로 장소를 변환했으며, 근거는 bundle의 `place_registration.json`에 있습니다. 약 1.5m 구간의 국소 정합이므로 장거리 절대 위치 정확도는 별도로 검증해야 합니다. 다른 LiDAR 장소 좌표는 가져오지 않았습니다.

`mapping origin`과 등록 장소는 현재 로봇 위치의 증거가 아닙니다. 실제 현재 위치는 cuVSLAM 재인식 결과에서 읽습니다. 현재 camera profile의 초기 탐색 힌트는 지도 원점, 수평 반경은 1m입니다. 수동으로 멀리 이동한 뒤 localization을 다시 시작하면 실제 위치에 맞는 `/initialpose` 힌트가 필요할 수 있습니다. 장소 등록용 좌표 정합이나 평가용 ground truth를 자동으로 localization 입력에 보내지 않습니다.

CoEgent의 **`--map-scene`**과 executor JSON의 **`scene_dir`**가 같은 bundle을 선택해야 합니다. 미일치, 계획 후 격자/원본 지도 변경, 카메라 프로필 또는 외부 SLAM controller의 profile/maps_root 미일치는 실행 전에 거절합니다. 지도별 계약과 기본 Plan DB를 분리했습니다. 같은 지도 UUID라도 격자/장소를 다시 만들면 계약이 바뀌므로, 기존 평가 DB를 덮어쓰지 말고 새로운 `--plan-db`를 사용해야 합니다. 기본값은 읽기 전용이며 이동 설정은 사용자가 명시합니다.

실행 예제는 `SUCCESS`를 자연어 목표 검증으로 간주하지 않습니다. 단일 짧은 주행의 통과 여부는 최종 실모델 보고서를 참고하고, 다른 시작 위치·장면 전체·장거리 주행으로 일반화하지 마세요.

`config/read_only.json`은 이동과 localization 시작을 차단합니다. 별도 `config/simulation.json`은 이동 허용과 자동 초기화를 켭니다. 사용자가 이동 시험을 시작할 때만 다음처럼 선택합니다.

```bash
bash A2_Execution_Adapter/scripts/run_a2_isaac.sh \
  --executor-options A2_Execution_Adapter/config/simulation.json \
  --command 'reception으로 이동해.'
```

이 명령은 사전에 등록된 reception 좌표를 대상으로 실행합니다. `auto_initialize=true`이면 자신의 localization을 시작하고 Nav2를 활성화하려고 시도합니다. 유효한 지도 TF, 정상 안전 상태, 최신 배터리/clock이 확보되기 전에는 이동 목표를 보내지 않습니다. `allow_motion`과 `auto_initialize`는 사용자/상위 에이전트가 설정하는 배포 옵션이며 LLM이 바꿀 수 없습니다.

초기 LiDAR 설정 시험에서 GUI는 `--no-tags`, localization=`STOPPED`, controller=`unconfigured`, safety=`EMERGENCY:ODOM_TIMEOUT`이었습니다. 해당 LiDAR 설정은 현재 cuVSLAM 서비스와 호환되지 않습니다. 위 cuVSLAM bundle을 선택하면 native 카메라 재인식으로 초기화합니다. Adapter는 simulator ground truth나 저장 장소 좌표를 현재 위치로 꾸며 넣지 않습니다. timeout은 실패로 보고합니다.

처음 구현 검증은 배터리/카메라만 포함했습니다. 이후 cuVSLAM 선택 시험은 [별도 결과 디렉토리](results/cuvslam_config_20261019/)에 보관합니다. 주행 중 사용자 취소와 장거리 임무는 별도 검증 대상입니다.

## 수동 이동 후 시뮬레이터 위치로 미션 실행

현재 Office 왕복 시험의 simulation pose 설정은 `maps/cuvslam360_9230e3f6_office_glass` 사본을 선택합니다. 원본 camera grid/MDB/PCD는 보존하며, Office USD에서 확인한 북쪽 유리벽과 열린 유리문 날개의 통행 금지 구역만 이 사본의 메타데이터에 추가했습니다(0.75m 여유). 이 정보는 cuVSLAM이 관측한 장애물이 아니라 시뮬레이션 자산에서 명시적으로 보완한 정적 지도 정보입니다. 유리문 중앙 sim_world(-17,51.012)을 통해 우회합니다.

낮은 시뮬레이션 배속에서는 별도의 시간 기준 문제도 확인했습니다. 원본 `unitree_cmd_vel_driver`의 10Hz 전송 제한은 ROS 시간, actuation의 0.5초 명령 만료는 실제 시간입니다. 이번 시험은 해당 driver의 `use_sim_time`만 일시적으로 false로 바꾸어 전송 간격을 실제 시간 기준으로 유지하고 종료 후 원복합니다. 속도/안전 임계값과 source 파일은 바꾸지 않습니다. 이것은 adapter의 기본 자동 설정이 아니며, 시험 기록 `50_driver_clock_override.json`에서 복원 여부를 확인해야 합니다.


사용자가 명시적으로 선택한 별도 시험 경로입니다. `config/isaac_ground_truth_simulation.json`의 `pose_source="isaac_ground_truth"`를 사용하면 Office의 현재 `/evaluation/ground_truth`를 scene에 저장된 고정 좌표 정합으로 변환하여 TF·odometry를 제공합니다. 원점에서 시작한다고 가정하지 않습니다. **이 모드는 cuVSLAM 재인식 시험이 아니며, 위치 정확도를 독립 평가하는 시험도 아닙니다.** 기본 pose source는 계속 cuVSLAM입니다.

```bash
# 별도 bridge 사본을 처음 만들거나 원본 버전 변경 후 다시 빌드할 때
bash A2_Execution_Adapter/scripts/build_private_bridge.sh

bash A2_Execution_Adapter/scripts/run_a2_isaac.sh \
  --executor-options A2_Execution_Adapter/config/isaac_ground_truth_simulation.json \
  --map-scene A2_Execution_Adapter/maps/cuvslam360_9230e3f6_office_glass \
  --mission-timeout 3600 \
  --plan-db /tmp/coegent-office-current-map-plans.sqlite3 \
  --command 'WEST OPEN AREA로 갔다가 START POINT로 돌아와'
```

선택된 지도와 좌표 정합이 필요하고, 지도 밖/막힌 위치에서의 주행은 보장하지 않습니다. 실제 Office 노드·clock·위치 frame·최신성을 검사하며, native SLAM localization이 STOPPED이고 mapping이 유휴 상태일 때만 켜집니다. 실행 도중 다른 SLAM이 시작되거나 TF와 시뮬레이터 위치가 어긋나면 주행을 중단합니다. source `DepthCloud`와 기존 경로 추종기를 재사용하고, 종료 시 새로 받은 두 시뮬레이터 메시지에서 속도 0을 확인합니다. native localization status나 READY anchor를 발행하지 않으며 native cuVSLAM 설정을 변경하지 않습니다.

이 설정은 `private_path_bridge=true`로 작업 전용 경로 브리지를 실행합니다. 원본 브리지의 취소 전체 요청만 제거한 사본이며, 승인 전 취소는 원래의 승인 콜백에서 해당 목표 UUID로 처리합니다. 원본 소스·설치 바이너리·서비스 설정은 바꾸지 않습니다. 별도 빌드 산출물은 `runtime/private_bridge/`에 있고, 실행 전 원본·패치본·바이너리 해시를 대조합니다. 재빌드는 ROS 개발 이미지 안에서 `python3 /adapter/a2_execution_adapter/private_bridge.py /rig /runtime/private_bridge`로 수행합니다.

목표·취소·응답은 작업마다 다른 `/coegent/t…/…` 토픽을 사용합니다. 느린 시뮬레이션에서 우회 경로가 계속 교체되는 문제를 줄이도록 전용 브리지만 stall 30초·cooldown 5초를 사용합니다. 상위 진행 감시는 `progress_timeout_s=1200`초입니다. 목표와 일시적으로 멀어지는 긴 우회 경로 및 약 0.15배속 시뮬레이션을 고려한 벽시계 기준 값이며, 기본 pose source의 30초는 바꾸지 않습니다. 시험용 전역 경로 반경은 실제 Nav2 footprint를 포괄하도록 `planning_radius_m=0.52`m를 사용합니다. 현재 소유 경로에서 새로운 `contact_blocked`가 나오면 즉시 취소합니다. 공유 FollowPath action의 활성 목표와 외부 경로 응답은 따로 감시하고, 다른 경로가 시작되면 우리 작업을 취소합니다. 응답 ID를 다른 브리지의 응답과 섞지 않습니다. `path_segment_m=4`는 전역 경로를 최대 4m 또는 35도 이상 모서리 전까지의 국소 목표로 전달합니다. 각 구간 뒤 현재 위치에서 다시 계획하며, 마지막 원래 API 목표 도착 검사까지 통과해야 해당 go_to가 성공합니다. 동일 action server에 동시에 요청하는 경쟁을 원자적으로 잠그는 서버 기능은 추가하지 않았습니다. `shortcut_margin_m=0.48`은 긴 직선으로 경로를 단축할 때만 추가 여유를 요구해 모서리를 너무 가깝게 자르는 것을 줄입니다. 기본 격자 통과 반경 .52m는 유지하므로 좁은 통로의 인접 격자 경로는 남습니다. 지도 밖 실제 충돌 여유를 보장하는 값은 아니며 실제 접촉 감시는 계속 유지됩니다.

## 열린 Isaac Office를 키보드로 조종

Office 쪽 기존 `joy_teleop.py`는 CoBiz 조이스틱 입력만 받으므로, 별도 터미널 키보드 컨트롤러를 추가했습니다. **현재 열린 office_sim을 직접 조종**합니다. Dangjin 브라우저 workbench는 사용하지 않습니다.

```bash
cd /home/gritandgrind/dev/pjh/local_ai_study
bash A2_Execution_Adapter/scripts/teleop_office.sh

# 읽기 전용 연결 확인: 조종 메시지 전송 없음
bash A2_Execution_Adapter/scripts/teleop_office.sh --check
```

Isaac GUI를 보이게 둔 채 **위 명령을 실행한 터미널에 키보드 포커스를 유지**합니다. W/S 전진·후진, A/D 좌·우 회전, Q/E 좌·우 옆걸음, Space 정지, X/Esc/Ctrl+C 종료입니다. 기본 전후진 0.3m/s, 옆걸음 0.2m/s, 회전 0.5rad/s입니다. 예를 들어 `--speed 0.2 --turn 0.3`으로 낮출 수 있습니다. 기존 드라이버의 실제 속도 한도를 넘는 설정은 거절합니다.

키를 누르고 있으면 터미널 자동 반복 입력으로 갱신합니다. 터미널은 key-up 이벤트를 제공하지 않으므로 마지막 반복 입력에서 **0.3초가 지나면 중앙 조이스틱(정지) 명령**을 보냅니다. 처음 누른 뒤 자동 반복이 시작되기 전에는 잠깐 끊길 수 있습니다. Space는 기다리지 않고 정지 명령을 보내며, 정상 종료·SIGINT/SIGTERM에서도 중앙 입력을 보낸 후 터미널 설정을 복원합니다. 이는 정지 명령 전송이며 실제 물리적 정지 ACK는 아닙니다. 프로세스가 강제 종료되거나 연결이 끊기면 기존 joystick/actuation/simulator watchdog이 남습니다.

```text
터미널 키 → sensor_msgs/Joy /a2_control (domain72)
          → 기존 joy_teleop → 기존 actuation_adapter → office_sim
```

컨트롤러는 실제 Office 노드와 진행하는 clock, joystick subscriber 및 라이브 parameter를 확인한 뒤 입력을 받습니다. 드라이버의 축 순서·방향·deadzone·속도 한도를 조회해 원하는 물리 속도로 인코딩합니다. 기존 source/서비스를 수정하거나 새 SLAM/Nav2/LLM을 띄우지 않습니다. 자율 주행과 수동 조종 사이의 명령 mux를 추가한 것은 아니므로 자율 임무를 정지한 상태에서 수동으로 사용합니다.

새 코드: `a2_execution_adapter/office_keyboard.py`, launcher: `scripts/teleop_office.sh`. 방향·속도 변환·입력 만료·잘못된 설정의 테스트 21개와 전체 회귀 **606개**가 통과했습니다. 실제 ROS 읽기 전용 연결 확인, 터미널 진입 및 X 종료도 확인했습니다. 이번 코드 검증에서 새 이동 명령은 발행하지 않았습니다. 증거는 `results/office_keyboard_20261019/`에 있습니다.

## Dangjin 환경을 키보드로 탐색

`/home/gritandgrind/dev/kts/dangjin-a2-sim`에는 키보드 조종이 이미 구현돼 있습니다. 이 디렉토리는 **Three.js + FastAPI 기반의 독립 브라우저 시뮬레이터**이며 현재 열린 Isaac office와 다른 환경입니다. 자체 수동 탐색에는 LLM/ROS/cuVSLAM이 필요하지 않습니다.

```bash
cd /home/gritandgrind/dev/pjh/local_ai_study
bash A2_Execution_Adapter/scripts/explore_dangjin.sh
# 브라우저: http://127.0.0.1:8876
```

기존 `dangjin-a2-sim/scripts/run_workbench.sh`를 포그라운드로 실행합니다. 8770의 기존 Mission Console과 충돌하지 않도록 기본 포트는 8876입니다. 데이터 초기 로딩에 수십 초가 걸릴 수 있습니다. 서버가 이미 실행 중이면 새로 띄우지 말고 URL을 엽니다. 포트 변경은 `A2SIM_WORKBENCH_PORT=8877 bash .../explore_dangjin.sh`로 지정할 수 있습니다. Ctrl+C로 해당 서버를 종료합니다.

브라우저에서 **3D 화면을 먼저 클릭**하고 키를 누릅니다. 입력창에 포커스가 있으면 조종 키를 처리하지 않습니다.

| 키/버튼 | 기능 |
|---|---|
| W / S | 전진 / 후진 |
| A / D | 좌회전 / 우회전 |
| Q / E | 왼쪽 / 오른쪽 옆걸음 |
| Space / Stop motion | 정지 |
| Drive speed | 속도 조절 |
| Whole site / Buildings | 전체 부지 / 건물 구역 보기 |
| Focus robot / Follow | 로봇 주변 보기 / 카메라 추적 |
| Saved start / Reset | 저장 시작 위치 선택 / 해당 위치로 초기화 |

기존 코드 위치:

- `dangjin-a2-sim/web/workbench/app.js`: keydown/keyup, 화면 포커스 상실 시 해제, `/api/cmd_vel` 전송.
- `dangjin-a2-sim/a2sim/workbench/app.py`: 수동 속도 API와 임무 중 조종 충돌 처리.
- `dangjin-a2-sim/a2sim/workbench/core.py`: 지형 추종 이동/충돌 처리/명령 만료.

환경에는 상단 사무실, 도로·경사 지형, 아래쪽 큰 건물/축사 및 다섯 저장 시작 위치가 있습니다. 약 284×448m 부지로, asset world는 실측 배치와 지형에 맞춘 근사 모델입니다. 실제 사족보행 접촉 동역학을 재현한 환경은 아닙니다. 기존 수동조종 API의 약 0.508rad 회전과 입력 해제 후 정지는 [확인 기록](results/dangjin_keyboard_20261019/manual_control_check.json)에 있습니다. 이 확인은 HTTP 제어 API 시험이며 브라우저 키 이벤트 자체를 자동화한 시험은 아닙니다.

**현재 Dangjin workbench에는 CoEgent의 cuVSLAM/Isaac Adapter를 연결하지 않았습니다.** 기존 선택 지도9230e3f6은 office 지도이므로 이 부지의 지도 대신 쓸 수 없습니다. workbench의 선택 ROS bridge도 domain42의 단일 카메라·가상 LiDAR·IMU 인터페이스이며, 기존 office의 domain72/스테레오 카메라/TF와 다릅니다. 이후 이 환경에서 cuVSLAM을 쓰려면 호환 카메라와 calibration, ROS 연결, 이 환경의 native 지도를 따로 맞춰야 합니다. 이번에는 사용자 수동 탐색을 먼저 준비했고 ROS bridge와 기존 Gemma 임무는 시작하지 않았습니다.

## 기존 계획을 LLM 없이 실행

계획 JSON 또는 `coegent plan`의 보고서 파일을 사용할 수 있습니다.

```bash
./.venv/bin/python -m a2_execution_adapter execute \
  --plan-file /absolute/path/to/plan-or-report.json \
  --options A2_Execution_Adapter/config/read_only.json \
  --out A2_Execution_Adapter/results/provided_plan.json
```

상위 에이전트의 재전송을 구분하려면 `coegent request`에 고정된 요청 파일을 제공합니다.

```json
{
  "mission_id": "upper-agent-unique-mission-id",
  "revision": 1,
  "expires_at": 1900000000,
  "command": "배터리 조회",
  "plan": {
    "schema_version": "planner.v1",
    "goal": "배터리 조회",
    "sequence": [{"type":"skill","name":"RobotBackend.check_battery","args":{}}],
    "success_criteria": ["배터리 API 정상 반환"],
    "final_outcome": "execute"
  }
}
```

`expires_at`은 UTC Unix 초입니다. 위 숫자는 형식 예시이므로 실제 요청의 유효기간으로 설정합니다.

```bash
./.venv/bin/python -m coegent request \
  --tool-profile a2-isaac --executor a2-isaac \
  --executor-options A2_Execution_Adapter/config/read_only.json \
  --request-file /absolute/path/to/request.json \
  --out A2_Execution_Adapter/results/request.json
```

이 경로는 모델을 로딩하지 않습니다. 같은 ID/revision/전체 요청은 저장된 결과를 `replayed=true`로 반환하고 다시 실행하지 않습니다. 같은 식별자로 본문/옵션을 바꾸면 충돌로 거절합니다. 더 낮은 미실행 revision도 거절합니다. 명시적으로 다시 실행하려면 새로운 mission_id를 사용합니다. standalone `execute`의 만료 시각은 매번 생성되므로 재전송에는 위의 고정 request 파일을 사용합니다.

## API 및 결과 변환

- `RobotBackend.go_to`: 실제 경로 계산/추종. False, 도착 위치 불일치, stale TF, 미초기화 상태는 성공으로 표시하지 않습니다.
- `RobotBackend.localize`, `stop_localization`, `ensure_nav_active`: 원래 API를 호출합니다. localization task ID는 고정하고 START/STOP 직전 소유권을 검사합니다. Nav2는 원래 스택과 같이 활성 상태로 남깁니다.
- `RobotBackend.refresh_pose`, `check_battery`, `observe`: 실제 읽기 API. 배터리는 0..1 비율입니다. pose의 속도 필드는 정지 증거로 사용하지 않습니다.
- `GridNavigator.plan`: 경로만 계산합니다. 결과 `value.path_xy`에 좌표를 반환합니다. 4,096포인트를 넘으면 `truncated=true`, 원래 `point_count`를 함께 반환합니다.
- `WorldState.capture_views`, `describe`: raw Python tuple/PIL 영상은 worker 내부에서 유지합니다. `describe.views={"$result":"camera","index":0}`는 앞선 `capture_views`의 실제 views 객체로 변환합니다. 부모 보고서에는 영상 크기/형식 등의 메타데이터만 넣습니다.

`WorldState.describe`에는 별도의 **이미지를 지원하는 VLM HTTP endpoint**가 필요합니다. 기본값은 KTS 소스 설정을 따르며 Adapter가 VLM 서버를 시작하지 않습니다. `vision_url`, `vision_model` 옵션으로 설정할 수 있습니다. 초기 검증에는 없었으나 후속 cuVSLAM 시험에서는 실제 호출/응답을 확인했습니다. 다만 그 시험은 분석 금지 명령을 모델이 어긴 사례이므로 의미 평가 실패이며, 응답 내용의 정확도를 입증하지 않았습니다. 카메라 획득 성공과 장면 분석 정확성은 별개입니다.

실행 조건은 실제 ROS에서 갱신한 `robot.battery_percent`, `robot.localization_valid`, `robot.estop`만 지원합니다. `robot.estop`은 여기서 외부 버튼만이 아니라 안전 계층의 EMERGENCY 상태를 포함하는 실행 차단 값입니다. 지원하지 않는 조건, `abort(return_home=true)`, 잘못된 참조나 인자는 **계획 전체 검증에서 먼저 거절**합니다. 새 관측으로 재계획이 필요하면 `REPLAN_REQUIRED`를 상위로 반환하며 숨은 LLM 재호출/이동 재시도는 하지 않습니다.

## 소유권과 상태 기록

Docker worker는 `coegent-a2-*` 이름으로 생성하고 종료 시 제거합니다. 기존 simulator와 Docker 서비스는 재시작하지 않습니다. 소스·패키지는 읽기 전용으로, `A2_Execution_Adapter/runtime`만 쓰기 가능하게 마운트합니다. GPU나 Docker socket을 worker에 전달하지 않습니다. ROS domain은 이 Isaac 환경의 **72**로 제한했습니다.

`runtime/*.lock`과 SQLite journal은 같은 Adapter의 동시 실행과 중복 실행을 막습니다. source root와 이미지/시간 제한 등은 `Options`에서 관리합니다. `runtime`은 계획 DB가 아니므로 성공 계획 목록과 섞이지 않습니다. 요청이 실행 도중 끊겨 결과가 불확실하면 새 이동을 `prior_motion_in_doubt`로 차단합니다. journal을 지워 자동 재실행하는 흐름은 제공하지 않습니다. 해당 요청의 실제 상태와 정지를 운영자가 확인해야 합니다. 읽기 전용 진단은 계속 가능합니다.

localization만 실패하고 경로 발행 전임이 기록된 경우에는 제한된 `reconcile` 명령이 있습니다. 기록된 task/map과 현재 `ERROR / process_exit`가 같을 때만 RESET을 발행하고, 새로운 STOPPED/reset 응답 후 이동 차단을 해제합니다. 다른 작업, 진행 중 주행, 소유권이 불명확한 기록에는 적용하지 않습니다. 원본 실패 보고서는 유지하고 복구 증거를 별도 테이블에 남깁니다. 소유권 필드 도입 전 기록에는 원래 host 보고서를 `--evidence-report`로 명시해야 하며, immutable journal과 내용이 일치하고 단일 START/no-path 이벤트가 있어야 합니다.

```bash
./.venv/bin/python -m a2_execution_adapter reconcile \
  --mission-id FAILED_MISSION_ID --revision 1 --task-id gemma-coegent-FAILED_TASK \
  --evidence-report /path/to/original-report.json --out /path/to/recovery.json
```

이 명령은 로봇 전체 정지나 모르는 요청의 강제 해제 기능이 아닙니다. 정상 실행에서는 자신의 ERROR/process_exit 세션에 같은 종료 확인을 적용합니다.

중단 확인은 **이 Adapter가 보낸 경로/시작한 localization**의 종료 응답을 뜻합니다. 다른 에이전트의 작업을 일괄 정지하지 않으며, 시스템 전체의 물리적 정지를 인증하는 값도 아닙니다. KTS의 localization STOP은 지도 단위 인터페이스여서 Adapter가 발행 직전 소유권을 검사합니다. 서로 다른 제어기가 같은 ROS 영역에서 동시에 명령하는 환경의 서버 측 원자적 소유권 보장까지 추가한 것은 아닙니다.

지도는 scene_meta의 실제 map UUID와 원본 metadata를 대조합니다. host/container 경로가 달라도 소스 파일 내용과 API 계약, scene_meta/map_scene 내용 해시를 확인합니다. 임의의 지도나 좌표계로 자동 대체하지 않습니다. 제어기 속도는 미션 전 값을 보존하고, 취소 중이던 동기 ROS 서비스가 끝난 뒤 원래 값으로 복원·확인합니다.

`SUCCESS`는 API 시퀀스 완료와 자신의 작업 정리 확인입니다. 자연어 success_criteria를 실제 목표 증거로 검증한 것은 아니므로 `goal_status=not_evaluated`, `success_criteria_verified=false`이고 성공 계획 자동 저장은 꺼져 있습니다. 이 프로필에서는 legacy `--state-mode strict`/관측 age 플래그 및 별도의 approved goals를 묵시적으로 적용하지 않고 거절합니다. 실제 상태 검사는 A2 runtime에서 수행합니다.

## 구조와 테스트

| 파일 | 역할 |
|---|---|
| `adapter.py`, `transport.py` | executor 플러그인, 소유한 Docker 프로세스와 JSON/cancel 통신 |
| `core.py` | API router, 결과 참조/변환, 기존 MissionEngine 실행 |
| `ros_runtime.py` | 실제 A2 API, map/TF/상태 검사와 소유 작업 정리 |
| `worker.py`, `journal.py` | ROS 작업 수명, EOF/신호 처리, 요청 replay/소유권 |
| `integration.py`, `__main__.py` | CoEgent run/request 및 standalone 진입점 |
| `binding.py`, `options.py` | 소스/지도 동일성, 배포 설정 |

```bash
/usr/bin/env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  PYTHONPATH=CoEgent_Agent:Executor_Adapters:A2_Execution_Adapter \
  ./.venv/bin/python -m pytest -q A2_Execution_Adapter/tests CoEgent_Agent/tests \
  CoEgent_Agent/evaluations/planner_sft_roles/test_grading.py
```

초기 검증은 **574개 테스트 통과**, 실제 E4B 배터리 조회·카메라 획득·중복 재실행 방지까지 확인했습니다. [초기 보고서](results/final_validation_20261019/REPORT.md)에 측정값이 있습니다. 이후 cuVSLAM 선택/복구 기능을 포함해 **585개 테스트 통과**, 실제 짧은 주행·도착·SLAM 종료를 확인했습니다. 전체 116.66초, native 도착 오차 0.29m였습니다. 후속 관측의 부정 지시 위반을 포함한 [최신 보고서](results/cuvslam_config_20261019/REPORT.md)를 함께 읽으세요. 사용자 계획 DB와 KTS 소스/지도는 수정하지 않습니다. 패키지를 제거하려면 venv에서 `pip uninstall a2-execution-adapter`를 실행합니다. CoEgent의 기본 mock 경로와 plan-only 경로는 그대로 사용할 수 있습니다.
