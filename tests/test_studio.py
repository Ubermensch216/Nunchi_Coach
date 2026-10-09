"""스튜디오 — 서식, 근거 대목으로 엮는 초안, 파일 내보내기."""

import hashlib
import json
import zipfile
from types import SimpleNamespace

import pytest
from lxml import etree

from app.core import notebook
from app.db import Database
from app.ingest.parsers import parse
from app.studio import compose, model, outputs, render, templates

SAMPLE = """# 청사 방호 용역 업무보고

## 1. 보고 배경 및 목적
- 청사 방호 용역 계약이 2026. 12. 31. 끝난다[1]
  - 후속 계약을 준비한다
## 2. 소요 예산
| 구분 | 금액 |
|---|---|
| 방호 인력 | 12,500,000원 |
> 확인 필요: 선택한 자료에서 이 항목의 근거를 찾지 못했습니다.

## 출처
- [1] 계약.txt · 1문단
"""


class Writer:
    """항목마다 정해 둔 문장을 내는 모델 대역."""

    def __init__(self, by_section=None, ready=True):
        self.by_section, self.ready, self.prompts = by_section or {}, ready, []
        self.cancelled = False

    def health(self):
        return SimpleNamespace(generation_ready=self.ready, embedding_ready=False)

    def generate_json(self, prompt, schema, num_predict=400, temperature=0.0):
        self.prompts.append(prompt)
        for title, sentences in self.by_section.items():
            if f"지금 쓸 항목: {title}" in prompt:
                return {"sentences": sentences}, None, ""
        return {"sentences": []}, None, ""


class Offline:
    def health(self):
        return SimpleNamespace(embedding_ready=False, generation_ready=False)


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "project" / "project.db")
    db.init()
    yield db
    db.close()


@pytest.fixture
def book(db, tmp_path):
    folder = tmp_path / "자료"
    folder.mkdir()
    source = db.add_source(folder)
    docs = []
    for name, text in (("계약.txt", "청사 방호 용역 계약기간은 2026. 12. 31.까지이다."),
                       ("예산.txt", "방호 용역 소요예산은 12,500,000원이다.")):
        path = folder / name
        path.write_text(text, encoding="utf-8")
        doc = db.upsert_document(source, {"path": str(path), "filename": name, "ext": ".txt",
                                          "hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                                          "parse_status": "ok"})
        db.replace_document_chunks(doc, [(1, "1문단", text)])
        docs.append(doc)
    key = notebook.create(db, "청사 방호")
    notebook.add_sources(db, key, docs)
    return key


def report(db):
    return next(t for t in templates.all_templates(db) if t.key == "work-report")


# ── 서식 ────────────────────────────────────────────────────────────

def test_builtin_templates_are_seeded_once_and_user_copies_do_not_touch_them(db):
    first = templates.all_templates(db)
    assert len(first) == len(templates.BUILTIN) and all(t.builtin for t in first)
    assert len(templates.all_templates(db)) == len(first)
    custom = report(db)
    custom.title = "우리 과 업무보고"
    custom.sections = custom.sections[:2]
    key = templates.save(db, custom)
    saved = templates.get(db, key)
    assert not saved.builtin and len(saved.sections) == 2 and len(report(db).sections) == 5
    with pytest.raises(ValueError):
        templates.delete(db, report(db).id)
    templates.delete(db, key)
    assert len(templates.all_templates(db)) == len(first)


# ── 본문 표기 ───────────────────────────────────────────────────────

def test_body_notation_is_split_into_blocks_without_losing_text():
    blocks = model.parse(SAMPLE)
    assert [b.kind for b in blocks] == ["title", "heading", "bullet", "bullet", "heading", "table", "note",
                                        "heading", "bullet"]
    assert blocks[3].level == 1 and blocks[5].rows == [["구분", "금액"], ["방호 인력", "12,500,000원"]]
    assert model.title_of(SAMPLE) == "청사 방호 용역 업무보고"
    assert model.cited(SAMPLE) == {1}                       # 출처 목록의 번호는 세지 않는다
    assert model.parse("그냥 한 줄\n이상한 | 표기")[1].kind == "paragraph"


# ── 초안 엮기 ───────────────────────────────────────────────────────

def test_draft_is_written_per_section_from_quoted_passages_only(db, book):
    notebook.query(db, book, "계약기간", Offline())
    notebook.query(db, book, "소요예산", Offline())
    writer = Writer({
        "1. 보고 배경 및 목적": [{"text": "청사 방호 용역 계약기간은 2026. 12. 31.까지이다.", "sources": [1]}],
        "2. 주요 추진 실적 및 현황": [
            {"text": "방호 용역 소요예산은 12,500,000원이다.", "sources": [2]},
            {"text": "소요예산은 99,000,000원으로 늘었다.", "sources": [2]}],      # 대목에 없는 숫자
    })
    seen = []
    draft = compose.compose(db, book, report(db), writer, on_progress=lambda *step: seen.append(step))
    assert draft.title == "청사 방호 업무보고" and [s[2] for s in seen][0] == "1. 보고 배경 및 목적"
    assert "- 청사 방호 용역 계약기간은 2026. 12. 31.까지이다.[1]" in draft.markdown
    assert "- 방호 용역 소요예산은 12,500,000원이다.[2]" in draft.markdown and "99,000,000" not in draft.markdown
    assert draft.markdown.count(model.MISSING) == 3 == draft.empty_sections
    assert [(c["index"], c["filename"]) for c in draft.citations] == [(1, "계약.txt"), (2, "예산.txt")]
    assert "- [2] 예산.txt · 1문단" in draft.markdown
    # 재료는 인용 대목이다. 검색 안내 문구 같은 답변 글은 프롬프트에 들어가지 않는다.
    assert "자료 검색 결과" not in writer.prompts[0] and "계약기간은 2026. 12. 31." in writer.prompts[0]


def test_repeated_sentences_are_merged_and_not_reused_in_later_sections(db, book):
    same = "청사 방호 용역 계약기간은 2026. 12. 31.까지이다."
    draft = compose.compose(db, book, report(db), Writer({
        "1. 보고 배경 및 목적": [{"text": same, "sources": [1]}, {"text": same, "sources": [1]}],
        "2. 주요 추진 실적 및 현황": [{"text": same, "sources": [1]}]}))
    assert draft.markdown.count(same) == 1
    assert draft.empty_sections == 4


def test_without_conversation_the_selected_sources_supply_the_passages(db, book):
    draft = compose.compose(db, book, report(db), Writer(
        {"1. 보고 배경 및 목적": [{"text": "방호 용역 소요예산은 12,500,000원이다.", "sources": [2]}]}))
    assert "대화 기록이 없어" in draft.note and draft.citations[0]["filename"] == "예산.txt"


def test_without_a_model_only_the_skeleton_is_made(db, book):
    draft = compose.compose(db, book, report(db), Writer(ready=False))
    assert draft.empty_sections == 5 and not draft.citations and "뼈대만" in draft.note


def test_draft_needs_selected_sources(db, book):
    notebook.select_all(db, book, False)
    with pytest.raises(ValueError):
        compose.compose(db, book, report(db), Writer())


# ── 저장·수정·내보내기 ──────────────────────────────────────────────

@pytest.fixture
def output(db, book):
    template = report(db)
    draft = compose.compose(db, book, template, Writer(
        {"1. 보고 배경 및 목적": [{"text": "청사 방호 용역 계약기간은 2026. 12. 31.까지이다.", "sources": [1]}]}))
    return outputs.save_draft(db, book, template, draft)


def test_output_is_saved_as_a_draft_and_edits_bump_the_revision(db, book, output):
    row = outputs.get(db, output)
    assert row["state"] == "ai_draft" and row["doc_type"] == "업무보고" and row["revision"] == 1
    outputs.update(db, output, outputs.payload(row)["markdown"].replace("# 청사 방호 업무보고", "# 2026년 방호 용역 보고"))
    row = outputs.get(db, output)
    assert row["title"] == "2026년 방호 용역 보고" and row["state"] == "edited" and row["revision"] == 2
    with pytest.raises(ValueError):
        outputs.update(db, output, "   ")


@pytest.mark.parametrize("kind", ["md", "docx", "hwpx", "pdf", "xlsx"])
def test_every_format_is_written_and_can_be_read_back(db, book, output, tmp_path, kind):
    outputs.update(db, output, SAMPLE.replace("[1] 계약.txt", "[1] 계약.txt"))
    target = outputs.export(db, output, tmp_path / "out" / f"보고.{kind}")
    assert target.is_file() and target.stat().st_size > 0
    logged = db.con.execute("SELECT * FROM output_files WHERE output_id=?", (output,)).fetchone()
    assert logged["format"] == kind and logged["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    read = parse(target)
    assert read.ok, read.error
    assert "청사 방호 용역 업무보고" in read.text and "12,500,000원" in read.text


def test_hwpx_package_has_the_expected_parts_and_wellformed_xml(db, book, output, tmp_path):
    outputs.update(db, output, SAMPLE)
    target = outputs.export(db, output, tmp_path / "보고.hwpx")
    with zipfile.ZipFile(target) as package:
        names = package.namelist()
        assert names[0] == "mimetype" and package.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert package.read("mimetype") == b"application/hwp+zip"
        for part in ("version.xml", "META-INF/container.xml", "Contents/content.hpf",
                     "Contents/header.xml", "Contents/section0.xml", "settings.xml"):
            etree.fromstring(package.read(part))
        section = package.read("Contents/section0.xml").decode("utf-8")
        header = package.read("Contents/header.xml").decode("utf-8")
    assert section.count("<hp:secPr") == 1 and "<hp:tbl" in section
    assert " ○ 청사 방호 용역 계약이" in section and "   - 후속 계약을 준비한다" in section   # 단계 기호와 앞 칸
    assert 'height="2000"' in header and 'face="맑은 고딕"' in header


def test_export_refuses_source_folders_unknown_formats_and_changed_evidence(db, book, output, tmp_path):
    with pytest.raises(ValueError):
        outputs.export(db, output, tmp_path / "자료" / "보고.docx")
    with pytest.raises(ValueError):
        outputs.export(db, output, tmp_path / "보고.txt")
    (tmp_path / "자료" / "계약.txt").write_text("바뀐 원문", encoding="utf-8")
    with pytest.raises(ValueError) as changed:
        outputs.export(db, output, tmp_path / "보고.docx")
    assert "원문이 달라졌" in str(changed.value)
    # 그 근거를 쓰지 않게 본문을 고치면 내보낼 수 있다.
    outputs.update(db, output, "# 보고\n\n직접 다시 쓴 본문\n")
    assert outputs.export(db, output, tmp_path / "보고.docx").is_file()


def test_format_settings_reach_the_files(db, book, output, tmp_path):
    fmt = json.loads(json.dumps(templates.DEFAULT_FORMAT))
    fmt["levels"][0].update(glyph="□", lead=0)
    fmt["title"]["size"] = 24
    text = render.to_hwpx(SAMPLE, fmt, "제목").__len__() and zipfile.ZipFile(
        __import__("io").BytesIO(render.to_hwpx(SAMPLE, fmt, "제목"))).read("Contents/section0.xml").decode("utf-8")
    assert "□ 청사 방호 용역 계약이" in text
    header = zipfile.ZipFile(__import__("io").BytesIO(render.to_hwpx(SAMPLE, fmt, "제목"))).read(
        "Contents/header.xml").decode("utf-8")
    assert 'height="2400"' in header


def test_pdf_embeds_only_the_glyphs_it_uses(db):
    data = render.to_pdf(SAMPLE, templates.DEFAULT_FORMAT, "제목")
    assert len(data) < 400_000
