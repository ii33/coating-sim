from coat_sim.ui.project_bbs_markdown import render_markdown


def test_report_markdown_renders_structural_formatting_without_active_content():
    result = render_markdown('# 研究报告\n\n**结论** 与 *说明*\n- 一项\n- 二项\n> 引用\n```python\nprint("<img src=https://bad>")\n```\n| 参数 | 结果 |\n| --- | --- |\n| 膜厚 | 待验证 |')
    for tag in ('<h1>', '<strong>', '<em>', '<ul>', '<blockquote>', '<pre>', '<table'):
        assert tag in result
    assert '<img' not in result and '&lt;img' in result


def test_html_links_remote_images_and_references_cannot_emit_resource_requests():
    result = render_markdown('<img src="file:///etc/passwd"><script>alert(1)</script>\n![x](https://evil/image.png)\n![ref][id]\n[id]: file:///secret\n[链接](javascript:alert(1))\n<iframe src="https://evil">')
    assert '<img' not in result and '<script' not in result and '<iframe' not in result
    assert '<a ' not in result
    assert '&lt;script&gt;' in result


def test_preview_bound_does_not_change_original_text():
    original = '长' * 70000
    result = render_markdown(original)
    assert len(original) == 70000
    assert '预览已截断' in result
    assert result.count('长') == 65536
