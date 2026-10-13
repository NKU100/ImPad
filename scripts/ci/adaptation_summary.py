"""Render terminal adaptation outcomes using evidence and an optional agent summary."""
import json

FIELDS = ('completed', 'blockers', 'nextActions', 'questions')

def read_summary(path):
    try:
        if path.stat().st_size > 32768:
            return {}
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or any(not isinstance(value.get(key), list) for key in FIELDS):
            return {}
        if any(not isinstance(item, str) for key in FIELDS for item in value[key]):
            return {}
        return {key: [item[:1000] for item in value[key][:8]] for key in FIELDS}
    except (OSError, ValueError):
        return {}

def render_result(app, version, status, run_url, summary=None, checkpoint=''):
    labels = {'verified':'适配成功', 'blocked':'适配阻塞', 'interrupted':'执行中断', 'running':'适配进行中'}
    lines = [f"**{labels.get(status, '等待运行时验收')}｜{app} {version}**", '']
    if status in {'verified', 'blocked'}:
        summary = summary or {}
        for field, title in [('completed','已完成'), ('blockers','当前阻塞'), ('nextActions','下一步'), ('questions','需要你操作')]:
            entries = summary.get(field, [])
            if entries:
                lines.extend([f'**{title}：**', '', *['- ' + entry.replace('<!--', '&lt;!--') for entry in entries], ''])
        lines.append('独立验收已通过。' if status == 'verified' else '没有新的有效评论或新版 APK 时，流水线将等待。')
    elif status == 'interrupted':
        lines.append('本次执行中断。以最后成功上传的检查点为准；中断不表示适配通过。')
    else:
        lines.append('正在适配或等待运行时验收，尚未确认成功。')
    if checkpoint:
        lines.extend(['', checkpoint])
    lines.extend(['', f'[查看 CI 运行]({run_url})'])
    return '\n'.join(lines) + '\n'
