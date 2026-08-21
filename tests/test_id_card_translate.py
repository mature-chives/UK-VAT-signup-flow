import unittest

from workbench.id_card_translate import (
    romanize_name,
    translate_authority,
    translate_birth_date,
    translate_id_card_fields,
    translate_place,
    translate_valid_period,
)


class TranslateIdCardTests(unittest.TestCase):
    def test_romanize_given_name_is_one_capitalized_token(self) -> None:
        self.assertEqual(romanize_name("赵君楚"), "Zhao Junchu")
        self.assertEqual(romanize_name("潘涵涵"), "Pan Hanhan")

    def test_birth_date_uses_english_month(self) -> None:
        self.assertEqual(translate_birth_date("1973年8月7日"), "7 August 1973")
        self.assertEqual(translate_birth_date("2006年8月10日"), "10 August 2006")

    def test_valid_period_uses_double_dash(self) -> None:
        self.assertEqual(
            translate_valid_period("2026.08.10-2031.08.10"),
            "2026.08.10 -- 2031.08.10",
        )

    def test_authority_follows_sub_bureau_pattern(self) -> None:
        self.assertEqual(
            translate_authority("北京市公安局东城分局"),
            "Public Security Sub-Bureau of Dongcheng District, Beijing",
        )
        self.assertEqual(
            translate_authority("北京市公安局"),
            "Public Security Bureau of Beijing",
        )

    def test_address_keeps_admin_units(self) -> None:
        self.assertIn("Beijing", translate_place("北京市东城区幽静街111号"))
        self.assertIn("Dongcheng District", translate_place("北京市东城区幽静街111号"))
        self.assertIn("No. 111", translate_place("北京市东城区幽静街111号"))
        guangxi = translate_place("广西壮族自治区贵港市平南县上渡街道")
        self.assertIn("Guigang City", guangxi)
        self.assertIn("Pingnan County", guangxi)
        self.assertIn("Sub-district", guangxi)
        self.assertNotIn("Guigangshipingnan", guangxi)

    def test_full_field_pack_uses_english_labels_values(self) -> None:
        translated = translate_id_card_fields(
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
        self.assertEqual(translated["full_name"], "Pan Hanhan")
        self.assertEqual(translated["sex"], "Male")
        self.assertEqual(translated["ethnicity"], "Han")
        self.assertEqual(translated["identity_document_number"], "110101200608104339")
        self.assertEqual(translated["issuing_authority"], "Public Security Bureau of Beijing")


if __name__ == "__main__":
    unittest.main()
