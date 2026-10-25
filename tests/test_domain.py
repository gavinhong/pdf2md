import pytest

from pdf2md.domain import inspect_pdf, page_runs, parse_pages, safe_name


def test_page_selection_preserves_physical_order_and_deduplicates():
    assert parse_pages("5, 1-3，2", 10) == [1, 2, 3, 5]
    assert page_runs([1, 2, 3, 5, 8, 9]) == [[1, 2, 3], [5], [8, 9]]
    assert parse_pages("", 3) == [1, 2, 3]


@pytest.mark.parametrize(
    "expression,total", [("0", 3), ("4", 3), ("3-1", 3), ("1,", 3), ("1-301", 1000), ("", 301)]
)
def test_invalid_or_excessive_ranges(expression, total):
    with pytest.raises(ValueError):
        parse_pages(expression, total)


def test_pdf_validation(pdf_path, tmp_path):
    assert inspect_pdf(str(pdf_path))[2] == 3
    corrupt = tmp_path / "broken.pdf"
    corrupt.write_bytes(b"not a pdf")
    with pytest.raises(ValueError, match="无法读取"):
        inspect_pdf(str(corrupt))
    wrong = tmp_path / "document.txt"
    wrong.write_text("hello")
    with pytest.raises(ValueError, match="PDF"):
        inspect_pdf(str(wrong))


def test_output_names_are_windows_and_macos_safe():
    assert safe_name("CON") == "_CON"
    assert safe_name(" a:b/c. ") == "a_b_c"
    assert len(safe_name("文" * 200)) == 90
