import io
import unittest

from PIL import Image
from pypdf import PdfReader

from workbench.id_card_render import (
    PHOTO_BOX,
    build_id_card_pdf,
    crop_portrait,
    render_translation_front,
)
from workbench.id_card_translate import translate_id_card_fields


class IdCardPdfTests(unittest.TestCase):
    def test_pdf_has_translation_page_then_original_page(self) -> None:
        fields = translate_id_card_fields(
            {
                "full_name": "潘涵涵",
                "sex": "男",
                "ethnicity": "汉",
                "birth_date": "2006年8月10日",
                "address": "北京市东城区幽静街111号",
                "identity_document_number": "110101200608104339",
                "issuing_authority": "北京市公安局",
                "valid_period": "2026.08.10-2031.08.10",
            }
        )
        front = Image.new("RGB", (400, 252), (180, 200, 210))
        back = Image.new("RGB", (400, 252), (210, 200, 180))
        portrait = Image.new("RGB", (80, 100), (40, 80, 120))
        pdf = build_id_card_pdf(
            translated=fields,
            original_front=front,
            original_back=back,
            portrait=portrait,
        )
        reader = PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), 2)
        self.assertGreater(len(pdf), 2000)

    def test_front_card_includes_english_labels(self) -> None:
        card = render_translation_front(
            {
                "full_name": "Pan Hanhan",
                "sex": "Male",
                "ethnicity": "Han",
                "birth_date": "10 August 2006",
                "address": "Beijing, Dongcheng District",
                "identity_document_number": "110101200608104339",
            },
            Image.new("RGB", (80, 100), (10, 20, 30)),
        )
        self.assertEqual(card.size, (1000, 630))
        sample = card.getpixel((PHOTO_BOX[0] + 20, PHOTO_BOX[1] + 20))
        self.assertNotEqual(sample, (236, 240, 244))

    def test_crop_portrait_uses_right_side_when_ocr_misses_box(self) -> None:
        image = Image.new("RGB", (400, 252), (255, 0, 0))
        for x in range(280, 400):
            for y in range(40, 160):
                image.putpixel((x, y), (0, 200, 0))
        portrait = crop_portrait(image, None)
        self.assertIsNotNone(portrait)
        assert portrait is not None
        self.assertEqual(portrait.getpixel((portrait.width // 2, portrait.height // 2)), (0, 200, 0))


if __name__ == "__main__":
    unittest.main()
