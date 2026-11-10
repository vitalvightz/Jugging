"""Offline payload comparison against a git revision; no model calls.

Run: PYTHONPATH=.:tests .venv/bin/python tools/audit_stage2_payload.py
Uses the existing deterministic camp fixture (fixed clock/seed, sparring/goals).
The draft is intentionally minimal: these are handoff measurements, not live
model latency or generated-card quality measurements.
"""
import ast
import json
import logging
import subprocess
from pathlib import Path
import sys


BASELINE = sys.argv[1] if len(sys.argv) > 1 else '43db0aed'


def old_function(module, path, name):
    source = subprocess.check_output(['git', 'show', f'{BASELINE}:{path}'], text=True)
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), namespace)
    return namespace[name]


def sections(text):
    return {s.splitlines()[0]: s for s in text.split('\n\n---\n\n')}


def json_section(text, title):
    section = sections(text)[title]
    return json.loads(section.split('```json\n')[1].split('\n```')[0])


def main():
    sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / 'tests')]
    import pytest
    from test_late_camp_fight_week_handoff import _run
    from fightcamp.plan_pipeline_runtime import prime_plan_banks
    import fightcamp.stage2_payload as payload
    import api.structured_plan_generation as structured
    logging.disable(logging.CRITICAL)
    prime_plan_banks(logger=logging.getLogger('audit'))
    before_handoff = old_function(payload, 'fightcamp/stage2_payload.py', 'build_stage2_handoff_text')
    before_card = old_function(structured, 'api/structured_plan_generation.py', 'build_structured_plan_prompt')
    # Same chars/4 estimate as stage2_automation._estimated_input_tokens.
    def metrics(s):
        return {'chars': len(s), 'estimated_tokens_chars_div_4': (len(s) + 3) // 4}

    results = []
    for weeks in (2, 4, 8, 12):
        with pytest.MonkeyPatch.context() as patch:
            p, b, after = _run(weeks * 7, patch)
        draft = sections(after)['STAGE 1 DRAFT PLAN'].split('\n', 1)[1]
        before = before_handoff(stage2_payload=p, planning_brief=b, plan_text=draft)
        old_profile = json_section(before, 'ATHLETE PROFILE')
        new_profile = {**json_section(after, 'FINALIZER PACKET')['athlete_model'], **json_section(after, 'ATHLETE PROFILE')}
        assert old_profile == new_profile
        # All other JSON and all safety/selection/manifest/draft sections identical.
        for heading, section in sections(before).items():
            if heading == 'ATHLETE PROFILE' or heading.startswith('You are Stage 2'):
                continue
            assert section == sections(after)[heading], heading
        assert sections(after)[next(iter(sections(after)))].startswith(sections(before)[next(iter(sections(before)))])
        kwargs = dict(plan_markdown=draft, planning_brief=b)
        cb, ca = before_card(**kwargs), structured.build_structured_plan_prompt(**kwargs)
        # Only single-line JSON context/support whitespace may differ.
        def normalize(prompt):
            return '\n'.join(json.dumps(json.loads(line), sort_keys=True) if line.startswith('{') and line.endswith('}') else line for line in prompt.splitlines())
        assert normalize(cb) == normalize(ca)
        results.append({'weeks': weeks, 'planning_brief': metrics(json.dumps(b)), 'handoff_before': metrics(before), 'handoff_after': metrics(after), 'structured_before': metrics(cb), 'structured_after': metrics(ca), 'sections_before': {k: metrics(v) for k,v in sections(before).items()}, 'brief_sections': {k: metrics(json.dumps(v)) for k,v in b.items()}, 'selected_plan_sections': {k: metrics(json.dumps(v)) for k,v in json_section(before, 'FINALIZER PACKET')['selected_plan'].items()}, 'equivalence': 'lossless profile reconstruction; all other handoff sections unchanged; structured JSON values identical'})
    print(json.dumps({'baseline': BASELINE, 'fixtures': results}, indent=2))


if __name__ == '__main__':
    main()
