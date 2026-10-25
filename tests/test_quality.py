from pdf2md.quality import continuation_warnings, invalid_formulas, ocr_evidence


def test_formula_validation_preserves_valid_latex_and_identifies_bad_braces():
    assert not invalid_formulas(r"price is \$5; math $v = \frac{s}{t}$ and $$x^2$$")
    assert invalid_formulas(r"bad $x_{2$ after") == [r"$x_{2$"]


def test_ocr_evidence_never_fabricates_missing_confidence():
    page = {
        "page_size": [100, 200],
        "para_blocks": [
            {
                "lines": [
                    {
                        "spans": [
                            {
                                "type": "text",
                                "content": "blurred",
                                "score": 0.6,
                                "bbox": [10, 20, 80, 40],
                            },
                            {"type": "text", "content": "no score"},
                        ]
                    }
                ]
            }
        ],
    }
    warnings, scores = ocr_evidence(page, 3)
    assert scores == [0.6]
    assert warnings[0]["bbox"] == [0.1, 0.1, 0.8, 0.2]
    assert warnings[0]["page"] == 3
    assert "blurred" not in str(warnings)


def test_unmerged_continuation_has_page_and_region():
    table = "<table><tr><td>A</td><td>B</td></tr></table>"
    blocks = [
        {"type": "table", "page": 1, "bbox": [0.1, 0.8, 0.9, 0.95], "table_body": table},
        {"type": "table", "page": 2, "bbox": [0.1, 0.1, 0.9, 0.3], "table_body": table},
    ]
    assert continuation_warnings(blocks)[0]["previous_page"] == 1
