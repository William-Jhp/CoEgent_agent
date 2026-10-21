"""Cheap candidate features, not a natural-language meaning/safety classifier."""
import re
import unicodedata

PARTICLES = ('쪽으로', '에서는', '에게는', '으로는', '로는', '에는', '에서', '으로', '에게', '까지', '부터', '로', '에', '을', '를', '은', '는', '이', '가')
ACTIONS = {
    'navigate_to': ('이동', '가줘', '가세요', '가'),
    'return_home': ('복귀', '돌아와', '돌아가'),
    'get_pose': ('위치', '좌표', '포즈'),
    'get_battery': ('배터리', '잔량', '충전량'),
    'inspect_target': ('점검', '검사', '확인', '살펴'),
    'inspect_scene': ('주변', '둘러봐'),
    'open_valve': ('열어', '열기'), 'close_valve': ('닫아', '닫기'),
    'rotate': ('회전', '돌려'), 'stop': ('정지', '멈춰', '중지'),
    'pick_object': ('집어', '잡아'), 'place_object': ('놓아', '내려'),
}


def normalized(text):
    return unicodedata.normalize('NFKC', str(text)).casefold().strip()


def terms(text):
    text = normalized(text)
    words = set(re.findall(r'[\w]+', text, flags=re.UNICODE))
    expanded = set(words)
    for word in words:
        for suffix in PARTICLES:
            if len(word) > len(suffix) and word.endswith(suffix):
                expanded.add(word[:-len(suffix)])
        # A로/90도/target_1 같은 mixed-script tokens retain their literal parts.
        expanded.update(re.findall(r'[a-z][a-z0-9_]*|[-+]?\d+(?:\.\d+)?', word))
    for action, aliases in ACTIONS.items():
        if action in expanded or any(any(w == alias or (len(alias) >= 2 and w.startswith(alias))
                                         for w in words) for alias in aliases):
            expanded.add(action)
    return {word for word in expanded if word and len(word) <= 128}


def extract_features(command, state=None):
    if not isinstance(command, str) or not command.strip() or len(command) > 8000:
        raise ValueError('selector command must be nonempty and at most 8000 characters')
    text = normalized(command)
    state = state or {}
    robot, world = state.get('robot', {}), state.get('world', {})
    known = set(world.get('paths', {})) | set(world.get('targets', {}))
    known.update(value for key in ('home', 'location', 'holding')
                 if isinstance((value := robot.get(key)), str))
    targets = []
    for name in known:
        value = normalized(name)
        if value and re.search(r'(?<![a-z0-9_])'+re.escape(value)+r'(?![a-z0-9_])', text):
            targets.append(value)
    arguments = sorted(set(re.findall(r'[-+]?\d+(?:\.\d+)?', text)))[:16]
    return {'targets': sorted(set(targets))[:32], 'arguments': arguments,
            'keywords': sorted(terms(command))[:64]}


def plan_terms(plan, *texts):
    result = set()
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                result.update(terms(key));visit(item)
        elif isinstance(value, list):
            for item in value: visit(item)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            result.update(terms(value))
    visit(plan)
    for text in texts: result.update(terms(text))
    return result
