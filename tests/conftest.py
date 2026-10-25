import pypdfium2 as pdfium
import pytest

from pdf2md.service import ApplicationService


@pytest.fixture
def pdf_path(tmp_path):
    path = tmp_path / "测试 document.pdf"
    document = pdfium.PdfDocument.new()
    for _ in range(3):
        page = document.new_page(595, 842)
        page.close()
    document.save(path)
    document.close()
    return path


@pytest.fixture
def service(tmp_path):
    service = ApplicationService(settings_path=tmp_path / "settings.json")
    yield service
    service.shutdown()
