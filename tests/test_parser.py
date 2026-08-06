"""解析器测试。"""
from agent.parser.parser import Parser


def test_json_fence_tool_call():
    out = '```json\n{"tool": "terminal_execute", "params": {"command": "ls"}}\n```'
    p = Parser().parse(out)
    assert p.action_type == "tool_call"
    assert p.tool_name == "terminal_execute"
    assert p.params == {"command": "ls"}


def test_plain_json_think():
    p = Parser().parse('{"think": "分析中"}')
    assert p.action_type == "think"
    assert p.content == "分析中"


def test_json_final_answer():
    p = Parser().parse('{"final_answer": "完成了"}')
    assert p.action_type == "final_answer"
    assert p.content == "完成了"


def test_text_fallback():
    p = Parser().parse("Tool: file_ops\nInput: {\"action\": \"read\", \"path\": \"a.txt\"}")
    assert p.action_type == "tool_call"
    assert p.tool_name == "file_ops"
    assert p.params["path"] == "a.txt"


def test_unknown_json_is_error():
    p = Parser().parse('{"hello": 1}')
    assert p.action_type == "error"
    assert p.error is not None


def test_retry_feedback_records_failure():
    parser = Parser(max_retries=3)
    p = parser.parse('{"hello": 1}')
    feedback = parser.retry_feedback(p, attempt=1)
    assert "无法解析" in feedback
    assert len(parser.failures) == 1
    assert parser.failures[0]["attempt"] == 1