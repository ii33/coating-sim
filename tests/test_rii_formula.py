"""RII 1–9 formula fixtures supplied from fixed official database snapshots."""
import math
import unittest

from coat_sim.core.optical_constants import (
    evaluate_optical_set, evaluate_optical_set_with_details, optical_model_catalog,
    validate_optical_set,
    prepare_optical_set,
)


CASES = [
    ("SiO2/Malitson", 1, [0,.6961663,.0684043,.4079426,.1162414,.8974794,9.896161], [.21,6.7],
     [(.21,1.5383576204905379),(.55,1.4599108864687284),(.5876,1.458462342053241),(1.55,1.444023621703261),(6.7,1.1596494139777271)]),
    ("N-BK7", 2, [0,1.03961212,.00600069867,.231792344,.0200179144,1.01046945,103.560653], [.3,2.5],
     [(.3,1.5527702635738705),(.55,1.5185223876207928),(.5876,1.5167984379050087),(1.55,1.5006520430195948),(2.5,1.4860177095710362)]),
    ("CDGM F5", 3, [2.56837524,-.00914046228,2,.0225979637,-2,.000999853488,-4,-3.66330712e-5,-6,4.93324803e-6,-8], [.365,1.014],
     [(.365,1.6713111440430433),(.55,1.628037657924798),(.5876,1.6243499843917803),(1.014,1.606820400168525)]),
    ("ZnTe/Li", 4, [9.92,.42530,0,.37766,2,8414.13,0,56.5,2], [.55,30],
     [(.55,3.1534407915110493),(.6328,2.988903228714393),(1,2.789162067848258),(10,2.6838817029677186),(30,2.499957324287212)]),
    ("D2O/Sarkar", 5, [1.31914,3.36189302e-3,-2,-8.57651e-5,-4], [.48,.71],
     [(.48,1.332115905652609),(.55,1.3293164364570726),(.6,1.3278168239753086),(.71,1.325471596762064)]),
    ("CO2/Old", 6, [0,1.54489e-6,.0584738,.083091927,210.9241,.0028764190,60.122959], [.480126,1.817228],
     [(.480126,1.0004534161409973),(.55,1.0004503649504738),(1,1.0004428295394717),(1.817228,1.0004362705436934)]),
    ("Si/Edwards", 7, [3.41983,.159906,-.123109,1.26878e-6,-1.95104e-9], [2.4373,25],
     [(2.4373,3.4433614523817818),(3,3.4361346775277178),(5,3.426066495556221),(10,3.421524557665201),(25,3.4201164083752955)]),
    ("TlCl/Schroter", 8, [.47856,.07858,.08277,-.00881], [.43,.66],
     [(.43,2.4244695187896658),(.5,2.320792515499418),(.55,2.2831651373670558),(.66,2.237192619481942)]),
    ("urea/Rosker-e", 9, [2.51527,.0240,.0300,.020,1.52,.8771], [.3,1.06],
     [(.3,1.7043928702073146),(.532,1.6122841802089926),(.8,1.5950847564233001),(1.06,1.5902092382376288)]),
    ("formula1_nonzero_offset", 1, [.25,.8,.2,.3,2.5], [.4,1.0],
     [(.4,1.5194686220082506),(.55,1.4685524334952906),(1,1.4234431763124498)]),
    ("formula2_negative_resonance_square", 2, [.25,.8,-.04,.3,6.25], [.4,1.0],
     [(.4,1.371903140386006),(.55,1.3933093091937159),(1,1.400745484407468)]),
    ("formula3_fractional_power", 3, [2.2,.003,2,.01,-2,.0002,.5], [.4,1.0],
     [(.4,1.504362486605674),(.55,1.4946951780244062),(1,1.4876827618817125)]),
    ("formula4_all_groups_fractional_power", 4, [2.2,.05,1.5,.1,2,.02,2,2,3,-.001,2,.002,-2], [.4,1.0],
     [(.4,1.5153412952222515),(.55,1.5083925363838144),(1,1.4995492348195532)]),
    ("formula5_fractional_power", 5, [1.4,.01,-2,.0002,.5], [.4,1.0],
     [(.4,1.4626264911064067),(.55,1.4332061752094113),(1,1.4102)]),
    ("formula6_nonzero_offset", 6, [.0001,.03,100,.01,50], [.4,1.0],
     [(.4,1.0006485714285714),(.55,1.0006244157022918),(1,1.0006071119356834)]),
    ("formula7_nonzero_sixth", 7, [1.4,.02,.0001,.001,-.0001,.00001], [.4,1.0],
     [(.4,1.5574118427598163),(.55,1.4744805067823333),(1,1.4215919759860455)]),
    ("formula8_synthetic", 8, [.25,.05,.01,.001], [.4,1.0],
     [(.4,1.5189500687084396),(.55,1.5159378471351836),(1,1.5149088208127265)]),
    ("formula9_synthetic", 9, [2.25,.01,.04,.02,1.2,.1], [.4,1.0],
     [(.4,1.5204314228901323),(.55,1.504398502830988),(1,1.49393615596358)]),
]


def rii_record(case):
    name, number, coefficients, limits, _points = case
    return {
        "id": "rii_" + name.lower().replace("/", "_").replace(" ", "_"),
        "name": name,
        "kind": "rii_formula",
        "formula_number": number,
        "coefficients": coefficients,
        "wavelength_unit": "um",
        "wavelength_min": limits[0], "wavelength_max": limits[1],
        "source": "fixed refractiveindex.info snapshot fixture",
        "notes": "Independent equation fixtures; values are model outputs, not new measurements.",
    }


class RIIFORMulaTests(unittest.TestCase):
    def test_prepared_snapshot_matches_scalar_api_and_detaches_mutable_coefficients(self):
        for case in CASES:
            record = rii_record(case)
            prepared = prepare_optical_set(record)
            for wavelength_um, expected_n in case[4]:
                with self.subTest(dataset=case[0], wavelength=wavelength_um):
                    expected = evaluate_optical_set_with_details(record, wavelength_um * 1000, "nm")
                    self.assertEqual(prepared(wavelength_um * 1000, "nm"), expected)
                    self.assertAlmostEqual(expected[0], expected_n, delta=2e-12)
            first_wave = case[4][0][0]
            original = prepared(first_wave, "um")
            record["coefficients"][0] += 0.125
            self.assertEqual(prepared(first_wave, "um"), original)

    def test_prepared_independent_k_and_per_query_errors_remain_strict(self):
        record = rii_record(CASES[1])
        record["k_data"] = {"wavelength_unit": "nm", "wavelength_min": 400, "wavelength_max": 700,
                            "wavelengths": [400, 550, 700], "k": [.1, .04, .02],
                            "source": "independent k fixture", "notes": "separate domain"}
        prepared = prepare_optical_set(record)
        for wave in (280, 450, 550, 2600):
            for include_k in (True, False):
                self.assertEqual(prepared(wave, "nm", "endpoint_hold", include_k),
                    evaluate_optical_set_with_details(record, wave, "nm", "endpoint_hold", include_k))
        for wave, unit, policy, include_k in [(280, "nm", "error", True),
                (550, "bad-unit", "error", True), (550, "nm", "bad-policy", True),
                (550, "nm", "error", 1), (float('nan'), "nm", "error", True)]:
            with self.subTest(wave=wave, unit=unit, policy=policy):
                with self.assertRaises(ValueError):
                    prepared(wave, unit, policy, include_k)

    def test_all_67_reference_points_and_nm_conversion(self):
        self.assertEqual(sum(len(case[4]) for case in CASES), 67)
        for case in CASES:
            record = rii_record(case)
            normalized = validate_optical_set(record)
            self.assertEqual(normalized["coefficients"], case[2])
            for wavelength_um, expected_n in case[4]:
                with self.subTest(dataset=case[0], formula=case[1], wavelength_um=wavelength_um):
                    n_um, k_um = evaluate_optical_set(record, wavelength_um, "um")
                    n_nm, k_nm = evaluate_optical_set(record, wavelength_um * 1000, "nm")
                    self.assertAlmostEqual(n_um, expected_n, delta=2e-12)
                    self.assertAlmostEqual(n_nm, expected_n, delta=2e-12)
                    self.assertIsNone(k_um)
                    self.assertIsNone(k_nm)

    def test_old_cauchy_and_sellmeier_remain_numerically_compatible(self):
        old_cauchy = {"id":"old_cauchy","name":"old Cauchy","kind":"cauchy",
            "wavelength_unit":"um","wavelength_min":.4,"wavelength_max":1,
            "parameters":{"A":1.4,"B":.01,"C":.0002}}
        rii_cauchy = {**old_cauchy,"kind":"rii_formula","formula_number":5,
            "parameters":None,"coefficients":[1.4,.01,-2,.0002,-4]}
        rii_cauchy.pop("parameters")
        p={"B1":.8,"C1":.04,"B2":.2,"C2":.01,"B3":.1,"C3":.2}
        old_sell={"id":"old_sell","name":"old Sellmeier","kind":"sellmeier",
            "wavelength_unit":"um","wavelength_min":.5,"wavelength_max":1,"parameters":p}
        sell_base={key:value for key,value in old_sell.items() if key != "parameters"}
        f1={**sell_base,"kind":"rii_formula","formula_number":1,
            "coefficients":[0,.8,.2,.2,.1,.1,math.sqrt(.2)]}
        f2={**sell_base,"kind":"rii_formula","formula_number":2,
            "coefficients":[0,.8,.04,.2,.01,.1,.2]}
        for wave in (.5,.55,.8,1):
            self.assertAlmostEqual(evaluate_optical_set(old_cauchy,wave,"um")[0], evaluate_optical_set(rii_cauchy,wave,"um")[0], delta=1e-13)
            self.assertAlmostEqual(evaluate_optical_set(old_sell,wave,"um")[0], evaluate_optical_set(f1,wave,"um")[0], delta=1e-13)
            self.assertAlmostEqual(evaluate_optical_set(old_sell,wave,"um")[0], evaluate_optical_set(f2,wave,"um")[0], delta=1e-13)

    def test_formula_schema_limits_finite_coefficients_and_catalog_contract(self):
        catalog = optical_model_catalog()
        rii = [item for item in catalog if item["kind"] == "rii_formula"]
        self.assertEqual([row["formula_number"] for row in rii], list(range(1,10)))
        for row in rii:
            self.assertEqual(row["coefficient_wavelength_unit"], "um")
            self.assertTrue(row["supports_independent_k"])
            self.assertEqual(row["coefficient_names"], [f"C{i}" for i in range(1,max(row["coefficient_counts"])+1)])
        base = rii_record(CASES[0])
        for broken in ({**base,"formula_number":True}, {**base,"formula_number":10},
                       {**base,"coefficients":["0",.6961663,.0684043,.4079426,.1162414,.8974794,9.896161]},
                       {**base,"coefficients":[0,.6961663,.0684043,float("nan"),.1162414,.8974794,9.896161]}):
            with self.subTest(broken=broken.get("formula_number"), coefficients=broken.get("coefficients")):
                with self.assertRaises(ValueError): validate_optical_set(broken)

    def test_inactive_singular_terms_are_skipped_before_power_or_pole_checks(self):
        cases = [
            (1,[0,0,.5],.5,1.0),
            (4,[2.25,0,0,.5,2,0,0,0,1],.5,1.5),
            (6,[0,0,4],.5,1.0),
            (7,[1.5,0,0],math.sqrt(.028),1.5),
            (8,[.25,0,.25,0],.5,math.sqrt(2)),
            (9,[2.25,0,.25,0,.5,0],.5,1.5),
        ]
        for number, coefficients, wavelength, expected in cases:
            record={"id":f"zero_{number}","name":f"zero_{number}","kind":"rii_formula",
                "formula_number":number,"coefficients":coefficients,"wavelength_unit":"um",
                "wavelength_min":.4 if wavelength>.4 else wavelength*.9,
                "wavelength_max":.6 if wavelength<.6 else wavelength*1.1}
            self.assertAlmostEqual(evaluate_optical_set(record,wavelength,"um")[0],expected,delta=2e-12)

    def test_independent_k_grid_is_retained_and_clamped_to_its_own_domain(self):
        record={"id":"formula_with_k","name":"N-BK7-like formula plus independent k",
            "kind":"rii_formula","formula_number":2,
            "coefficients":[0,1.03961212,.00600069867,.231792344,.0200179144,1.01046945,103.560653],
            "wavelength_unit":"um","wavelength_min":.3,"wavelength_max":2.5,
            "source":"n model source","notes":"n-only formula source",
            "k_data":{"wavelength_unit":"nm","wavelength_min":320,"wavelength_max":2400,
                "wavelengths":[320,600,2400],"k":[.1,.05,.02],
                "source":"independent measured k","notes":"independent range"}}
        n,k,details=evaluate_optical_set_with_details(record,350,"nm",extrapolation_policy="endpoint")
        self.assertGreater(n,1)
        self.assertAlmostEqual(k,.1 + (350-320)/(600-320)*(.05-.1))
        self.assertFalse(details["n_extrapolation_side"])
        self.assertIsNone(details["k_extrapolation_side"])
        self.assertEqual(details["n_evaluation_wavelength_nm"],350)
        self.assertEqual(details["k_evaluation_wavelength_nm"],350)
        n2,k2,details2=evaluate_optical_set_with_details(record,280,"nm",extrapolation_policy="endpoint")
        self.assertAlmostEqual(n2,evaluate_optical_set(record,300,"nm","endpoint")[0])
        self.assertAlmostEqual(k2,.1)
        self.assertEqual(details2["n_extrapolation_side"],"below")
        self.assertEqual(details2["k_extrapolation_side"],"below")
        self.assertEqual(record["k_data"]["wavelengths"],[320,600,2400])
        with self.assertRaisesRegex(ValueError,"有效波段"):
            evaluate_optical_set(record,280,"nm")
        _n,_k,disabled=evaluate_optical_set_with_details(record,280,"nm","endpoint",include_k=False)
        self.assertIsNone(_k)
        self.assertTrue(disabled["k_disabled"])
        self.assertTrue(disabled["k_known"])

    def test_declared_domains_reject_internal_rii_poles(self):
        cases = [
            (1, [0, .001, .5], .4, .6),
            (2, [0, .001, .25], .4, .6),
            (4, [1, .001, 0, 2, 0, 0, 0, 0, 0], .9, 1.1),
            (6, [0, .001, 1], .9, 1.1),
            (7, [1, .001], .16, .18),
            (8, [.5, 0, .3, 2], .4, .6),
            (9, [1, 0, .25, 1, .5, 0], .4, .6),
        ]
        for number, coefficients, low, high in cases:
            with self.subTest(formula=number):
                with self.assertRaisesRegex(ValueError, "极点"):
                    validate_optical_set({"id":f"pole_{number}","name":f"pole {number}",
                        "kind":"rii_formula","formula_number":number,
                        "coefficients":coefficients,"wavelength_unit":"um",
                        "wavelength_min":low,"wavelength_max":high})

    def test_k_data_validation_rejects_duplicate_or_negative_samples(self):
        base=rii_record(CASES[0])
        for waves,values in (([320,320],[.1,.2]),([320,600],[.1,-.1])):
            broken={**base,"k_data":{"wavelength_unit":"nm","wavelength_min":320,
                "wavelength_max":600,"wavelengths":waves,"k":values}}
            with self.assertRaises(ValueError): validate_optical_set(broken)

    def test_formula_domain_and_reported_endpoint_policy_do_not_extend_the_formula(self):
        record=rii_record(CASES[0])
        with self.assertRaisesRegex(ValueError,"有效波段"):
            evaluate_optical_set(record,200,"nm")
        n0,k0,d=evaluate_optical_set_with_details(record,200,"nm","endpoint")
        n1,k1=evaluate_optical_set(record,210,"nm")
        self.assertAlmostEqual(n0,n1,delta=1e-13)
        self.assertIsNone(k0); self.assertIsNone(k1)
        self.assertEqual(d["n_extrapolation_side"],"below")
        self.assertEqual(d["n_evaluation_wavelength_nm"],210)

    def test_formula8_transform_pole_and_formula9_denominators_fail(self):
        cases=[
            {"id":"f8pole","name":"f8pole","kind":"rii_formula","formula_number":8,
             "coefficients":[1,0,.2,0],"wavelength_unit":"um","wavelength_min":.4,"wavelength_max":.6},
            {"id":"f9pole","name":"f9pole","kind":"rii_formula","formula_number":9,
             "coefficients":[2,.1,.25,0,.5,0],"wavelength_unit":"um","wavelength_min":.4,"wavelength_max":.6},
        ]
        with self.assertRaises(ValueError): evaluate_optical_set(cases[0],.5,"um")
        with self.assertRaises(ValueError): evaluate_optical_set(cases[1],.5,"um")


if __name__ == "__main__":
    unittest.main()
