from mailagent.mail.parse import HTML_MAX, decode_header_value, html_to_text, parse_message


def test_unknown_charsets_degrade_instead_of_failing():
    assert decode_header_value("=?undefined?B?aGVsbG8=?=") == "hello"
    raw = (b"From: a@x.example\r\nSubject: =?undefined?B?aGVsbG8=?=\r\n"
           b"Content-Type: text/plain; charset=undefined\r\n\r\nbody text\r\n")
    m = parse_message(raw, mailbox_id="me", uid=1)
    assert m.subject == "hello" and "body text" in m.body_text


def test_html_input_is_capped():
    # html.parser 在超大输入上是超线性的（100 万字符要好几秒），正文反正只留 2 万字
    text = html_to_text("<p>start</p>" + "a" * HTML_MAX + "<p>tail</p>")
    assert text.startswith("start") and "tail" not in text
