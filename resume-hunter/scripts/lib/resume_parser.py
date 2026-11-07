"""简历解析模块 - PDF/Word → 纯文本"""

import logging
from pathlib import Path

logger = logging.getLogger("resume-hunter")


def parse_resume(file_path: str | Path) -> str:
    """解析简历文件，返回纯文本内容"""
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return _parse_pdf(path)
    elif suffix in (".docx", ".doc"):
        return _parse_docx(path)
    elif suffix == ".txt":
        return path.read_text(encoding="utf-8")
    else:
        logger.warning(f"不支持的简历格式: {suffix}")
        return ""


def _parse_pdf(path: Path) -> str:
    """解析 PDF 简历"""
    import pdfplumber

    text_parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                text_parts.append(text)
    return "\n".join(text_parts)


def _parse_docx(path: Path) -> str:
    """解析 Word 简历"""
    from docx import Document

    doc = Document(path)
    return "\n".join(para.text for para in doc.paragraphs if para.text.strip())
