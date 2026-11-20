"""Synthetic regressions for failure patterns found in public OEM documents."""

from types import SimpleNamespace

from autorepair.models import Block, Document
from autorepair.pdf_structure import restore_hierarchy
from autorepair.pdf_tables import header_markdown, recover_table


def test_multirow_headers_and_group_caption_preserve_labels():
    md = header_markdown(
        [
            ["Code", "Code", "Name", "Qty"],
            ["Previous", "New", "Name", "Qty"],
            ["A", "B", "Stopper", "2"],
        ]
    )
    assert "| Previous / Code | New / Code | Name | Qty |" in md
    assert "| A | B | Stopper | 2 |" in md
    md = header_markdown([["Model Z"] * 3, ["Part", "Code", "Date"], ["Motor", "X", "N/A"]])
    assert md.startswith("Model Z\n\n| Part | Code | Date |")
    # A genuine blank remains blank; do not invent shared values.
    assert "| Motor | X |  |" in header_markdown([["Part", "Code", "Date"], ["Motor", "X", ""]])


def test_sparse_outer_box_cannot_replace_three_column_table(monkeypatch, tmp_path):
    import autorepair.pdf_tables as module

    monkeypatch.setattr(module, "ruled_tables", lambda *args: [((0, 0, 100, 100), [["all"]])])
    box = SimpleNamespace(l=0, t=0, r=100, b=100)
    item = SimpleNamespace(
        prov=[
            SimpleNamespace(page_no=1, bbox=SimpleNamespace(to_top_left_origin=lambda **kw: box))
        ],
        data=SimpleNamespace(num_cols=3),
    )
    document = SimpleNamespace(pages={1: SimpleNamespace(size=SimpleNamespace(height=100))})
    assert recover_table(item, document, tmp_path / "source.pdf", "digest") is None


def test_native_section_tag_wins_over_repeated_running_header():
    chars = [
        dict(
            text=c,
            x0=i * 6,
            x1=i * 6 + 6,
            top=20,
            bottom=30,
            size=10,
            fontname="Bold",
            non_stroking_color=0,
            mcid=1,
        )
        for i, c in enumerate("REPAIR A")
    ]
    pdf = SimpleNamespace(
        pages=[SimpleNamespace(chars=chars, height=792)] * 2,
        structure_tree=[dict(type="H2", page_number=2, mcids=[1])],
    )
    doc = Document(
        doc_id="test",
        brand="test",
        source_name="test.pdf",
        source_hash="a",
        parser="test",
        blocks=[
            Block(
                block_id=f"b{i}",
                original="REPAIR A",
                text="REPAIR A",
                kind="heading",
                location={
                    "pages": [i],
                    "bboxes": [dict(l=0, t=20, r=48, b=30, coord_origin="TOPLEFT")],
                },
            )
            for i in [1, 2]
        ],
    )
    restore_hierarchy(doc, pdf)
    assert doc.blocks[1].kind == "heading"
    assert doc.blocks[1].heading_level == 2
    assert doc.blocks[1].location["heading_method"] == "pdf-tag"


def test_scanned_pdf_without_native_chars_keeps_ocr_heading():
    pdf = SimpleNamespace(pages=[SimpleNamespace(chars=[], height=792)], structure_tree=[])
    doc = Document(
        doc_id="scan",
        brand="test",
        source_name="scan.pdf",
        source_hash="x",
        parser="test",
        blocks=[
            Block(
                block_id="b1",
                kind="heading",
                original="1.1 Check",
                text="1.1 Check",
                location={"pages": [1]},
            )
        ],
    )
    restore_hierarchy(doc, pdf)
    assert doc.blocks[0].kind == "heading"
    assert doc.blocks[0].heading_level == 2


def test_export_preserves_step_number_and_real_markdown_bullet():
    from autorepair.models import Result
    from autorepair.pipeline import markdown

    doc = Document(
        doc_id="steps",
        brand="test",
        source_name="steps.pdf",
        source_hash="x",
        parser="test",
        blocks=[
            Block(block_id="b1", kind="list", original="Check", text="Check", list_marker="21."),
            Block(block_id="b2", kind="list", original="Inspect", text="Inspect", list_marker="·"),
        ],
    )
    md = markdown(Result(document=doc, rules_hash="test"))
    assert "\n21. Check" in md
    assert "\n- Inspect" in md


def test_native_small_icon_is_saved_once_with_page_provenance():
    from PIL import Image

    from autorepair.pdf_structure import preserve_unclassified_images

    image = dict(x0=20, x1=34, top=20, bottom=33)
    page = SimpleNamespace(
        images=[image, image],
        width=100,
        height=100,
        to_image=lambda **kw: SimpleNamespace(original=Image.new("RGB", (200, 200), "red")),
    )
    doc = Document(
        doc_id="icon", brand="test", source_name="icon.pdf", source_hash="x", parser="test"
    )
    preserve_unclassified_images(doc, SimpleNamespace(pages=[page]))
    assert len(doc.blocks) == 1
    assert len(doc.assets) == 1
    assert doc.blocks[0].location["pages"] == [1]
    assert doc.blocks[0].image_src.startswith("assets/")
    assert doc.issues[0].code == "image.native_fallback"
