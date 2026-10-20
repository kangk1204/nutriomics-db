"""Reproducible source-backed intervention curation; expert review remains pending."""
from __future__ import annotations

import json
from pathlib import Path

V="human-intervention-bridge-2026-10-01-v1"
ENERGY="FDC-NUTRIENT:1008"
FAT="FDC-NUTRIENT:1004"
PROTEIN="FDC-NUTRIENT:1003"
CARB="FDC-NUTRIENT:1005"
FIBER="FDC-NUTRIENT:1079"
EPA="FDC-NUTRIENT:1278"
DHA="FDC-NUTRIENT:1272"
SFA="FDC-NUTRIENT:1258"
PUFA="FDC-NUTRIENT:1293"


def exposure(key,label,kind,formulation,basis,keys=(),terms=(),amount=None,unit=None,days=None,**meta):
    result={"key":key,"label":label,"kind":kind,"formulation":formulation,"dose_basis":basis,
            "component_keys":list(keys),"food_terms":list(terms),"amount":amount,"unit":unit,
            "duration_days":days,"metadata":meta}
    if "contrast_pattern" in meta:
        result["contrast_pattern"]=result["metadata"].pop("contrast_pattern")
    return result


def study(sid,title,series,pmid,url,design,tissue,allocation,exposures,limits,n=None,assays=None,**meta):
    return dict(id=sid,title=title,series=series,pubmed_id=pmid,
        sources=[{"label":"Primary publication","url":url},
                 *[{"label":x,"url":"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc="+x}
                   if x.startswith("GSE") else {"label":x,"url":"https://www.ebi.ac.uk/biostudies/arrayexpress/studies/"+x}
                   for x in series]],description=title,design=design,analysis_design=meta.pop("analysis_design",design),
        tissue=tissue,allocation=allocation,exposures=exposures,limitations=limits,
        source_location="Primary publication Methods / intervention design; repository sample metadata; study-specific limitations below.",
        n_people=n,n_samples=assays,**meta)


def manifest():
    common="음식 matrix·여러 성분이 함께 달라진 중재이며 각 성분의 독립 효과를 식별하지 않는다."
    studies=[
        study("S01","고지방 식후 반복 혈액 RNA-seq",["GSE127530"],"31473510",
              "https://pubmed.ncbi.nlm.nih.gov/31473510/","randomized_supplement_crossover","whole blood","randomized",
              [exposure("meal","고지방 혼합 식사 challenge","mixed_meal","study-specific mixed high-fat meal",
                        "연구 식사 열량; 개인별 양의 정량 연결 미완료",[ENERGY,FAT,PROTEIN,CARB],frequency="single challenge on three visits"),
               exposure("dha","DHA 단회 보충 조건","compound","DHA supplement","single 2 g supplement challenge",
                        [DHA],amount=2,unit="g",contrast_pattern="a^",visit_mapping_status="unresolved")],
              [common,"RNA-seq 5명·45개 시료; 전체 임상/qPCR 대상자와 다르다.",
               "blueberry/DHA/placebo 방문–RNA 시료 대응 미확인; 보충제별 효과로 연결하지 않는다.",
               "보충제 차이의 비유의 결과는 동등성의 증명이 아니다."],5,45,analysis_design="repeated_meal_response"),
        study("S02","식사 열량·시간·체중군 혈액 발현",["GSE56960"],"28319853",
              "https://pubmed.ncbi.nlm.nih.gov/28319853/","randomized_meal_crossover","whole blood","randomized_meal_order",
              [exposure(str(k),f"고지방 혼합 식사 {k} kcal","mixed_meal","study-specific high-fat meal",
                        "assigned energy per challenge",[ENERGY,FAT,PROTEIN,CARB],amount=k,unit="kcal",
                        contrast_pattern=rf"(?:_{k}_|{k}vs500|{'_vs500_' if k==500 else 'a^'})",allocation_scope="meal order only",frequency="single challenge")
               for k in (500,1000,1500)],
              [common,"14명·168개 assay; 정상체중 7명/비만 7명. 체중군은 무작위배정이 아니다.",
               "식후 시간과 일중 변화의 혼입 가능성이 있어 식사만의 인과 효과로 해석하지 않는다."],14,168),
        study("S03","포도씨 flavanol 활성군 발현·DNAm",["GSE54690","GSE54325","GSE54643"],"24763279",
              "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0095527",
              "uncontrolled_public_active_arm","blood leukocytes","parent_randomized_public_active_only",
              [exposure("flavanols","포도씨 monomeric/oligomeric flavanols","component_group","grape-seed flavanol mixture",
                        "assigned 200 mg/day mixture",terms=["포도씨 추출물","grape seed extract","포도","grape"],
                        amount=200,unit="mg",days=56,reported_components={"total_catechins_mg":51.3,"total_dimers_mg":55,"polymers_mg":93.8},
                        food_relation_kind="source_plant_related",dose_frequency="per_day")],
              [common,"공개 활성군 전후 비교에는 무작위 대조군 자료가 없다.",
               "발현 7명/14개 생물학적 시료/7개 two-color 배열; DNAm 10명/20개 시료.",
               "개인 ID 대응 미확인; 개인 내 다중오믹스 분석 제외.",
               "총 catechin·dimer 함량은 개별 catechin 화합물 용량이 아니다.",
               "식품의 포도와 포도씨 농축 보충제는 투여 형태가 다르다."],None,None),
        study("S04","PREDIMED-Navarra 식이군별 장기 DNAm",["GSE107205"],"29295516",
              "https://www.mdpi.com/2072-6643/10/1/15","randomized_parent_selected_subset","peripheral white blood cells","parent_randomized_subset_audit_required",
              [exposure("evoo","지중해식 + EVOO","diet_pattern","Mediterranean diet supplemented with EVOO",
                        "배정 용량 미확인; 관찰 섭취량과 분리",[FAT],terms=["olive oil","올리브유"],
                        contrast_pattern="(?i)evoo|olive",reported_duration="5 years"),
               exposure("nuts","지중해식 + 견과 혼합물","diet_pattern","Mediterranean diet supplemented with mixed nuts",
                        "배정 용량 미확인; 단일 견과 용량 미확인",[FAT,FIBER],terms=["호두","아몬드","walnut","almond","hazelnut"],
                        contrast_pattern="(?i)nut",reported_duration="5 years"),
               exposure("lowfat","저지방 안내 대조군","diet_pattern","low-fat dietary advice",
                        "식단 안내; 단일 성분 배정 용량 없음",[FAT],contrast_pattern="(?i)low|control|lf",reported_duration="5 years")],
              [common,"36명(각 군12명) 완주 하위표본; 원 배정·하위선정·탈락 감사 필요.",
               "군 내 전후 변화와 군 간 변화 차이는 다른 대비이며 구분한다."],36,72),
        study("S05","NAFLD 지중해식·고섬유 단일군 DNAm",["GSE219217"],"36447285",
              "https://link.springer.com/article/10.1186/s13148-022-01377-6","uncontrolled_before_after","whole blood","single_arm",
              [exposure("fiber","지중해식 상담 + 고섬유 rolls 2개/day","diet_pattern","Mediterranean counselling plus high-fibre rolls",
                        "2 rolls/day; 질량과 fibre g는 미확인",[FIBER],reported_duration="30 and 60 days",
                        assigned_food_count=2,assigned_count_unit="rolls/day")],
              [common,"18명·54개 시료; 중재 대조군 없음.","시간·체중 변화·평균으로의 회귀와 중재를 분리할 수 없다."],18,54),
        study("N14","SFA/PUFA 식후 PBMC 지방산 종류 비교",["GSE13466"],"19923369",
              "https://pubmed.ncbi.nlm.nih.gov/19923369/","randomized_crossover","PBMC","randomized",
              [exposure("sfa","SFA-rich shake","mixed_meal","study-specific SFA-rich shake","shake fatty-acid dose pending source extraction",[SFA,FAT]),
               exposure("pufa","PUFA-rich shake","mixed_meal","study-specific PUFA-rich shake","shake fatty-acid dose pending source extraction",[PUFA,FAT])],
              [common,"21명·84개 배열; 공개 전장 발현은 SFA/PUFA만 해당.",
               "E-MTAB-65는 동일 자료이며 독립 연구가 아니다.","NuGO custom CDF를 보존한다; PBMC와 전혈을 합치지 않는다."],21,84),
        study("N15","요구르트/산성화 우유 교차시험 혈액 RNA-seq",["GSE98645"],"29489876",
              "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0192947","randomized_double_blind_crossover","whole blood","randomized",
              [exposure(f"{product}_{period}",f"{label} {amount}g {'단회' if period=='acute' else '/day·2주'}","product",formulation,
                        "single meal" if period=="acute" else "assigned per-day intake",[FAT,CARB,PROTEIN],terms=[label,product],
                        amount=amount,unit="g",days=0 if period=="acute" else 14,
                        contrast_pattern=r"[246]h_change" if period=="acute" else "2week_change",
                        dose_frequency="single" if period=="acute" else "per_day")
               for product,label,formulation in [("yogurt","요구르트","trial-specific probiotic yogurt"),("milk","우유","trial-specific acidified milk")]
               for period,amount in [("acute",800),("chronic",400)]],
              [common,"임상13명과 RNA-seq7명/77개시료를 구분.",
               "제품·발효·matrix 효과이며 개별 성분·균주의 독립 효과가 아니다.",
               "일반 우유/요구르트와 시험 제품의 동일성은 확인되지 않았다."],7,77),
        study("N16","DIRECT PLUS 18개월 식이 중재 DNAm",["E-MTAB-12527"],"37236302",
              "https://www.metabolismjournal.com/article/S0026-0495%2823%2900198-1/fulltext",
              "randomized_parallel_selected_paired_subset","whole blood","randomized",
              [exposure("walnuts","MED/greenMED의 호두 28g/day","diet_pattern","walnuts within calorie-restricted Mediterranean dietary pattern",
                        "assigned 28 g/day walnuts as part of whole dietary intervention",[FAT,PUFA,FIBER],terms=["호두","walnut"],
                        amount=28,unit="g",reported_duration="18 months",dose_frequency="per_day"),
               exposure("green","greenMED: green tea + Mankai + MED","diet_pattern","green Mediterranean diet plus physical activity guidance",
                        "green tea 3–4 cups/day; Mankai100g/day; whole-pattern intervention",terms=["녹차","green tea","Mankai"],
                        reported_duration="18 months",contrast_pattern="greenMED",
                        polyphenol_assay_basis="GAE equivalent; not sum of individual compound masses")],
              [common,"公開 SDRF256명·512시료·1024색상 채널. 논문260명과 차이 감사 필요.",
               "HDG88/MED81/greenMED87명의 완전한 전후 쌍.",
               "모든 군에 신체활동 안내; 식단 성분별 인과 분리는 불가능.",
               "RNA-seq166명·개인별 임상자료 접근 미확인. 동일인 다중오믹스 연결은 보류."],256,512),
        study("N06","fish oil/fenofibrate 교차시험 PBMC",["GSE27385"],"21429719",
              "https://pubmed.ncbi.nlm.nih.gov/21429719/","randomized_double_blind_crossover","PBMC","randomized",
              [exposure("fish_oil","fish oil 3.7g/day n-3","component_group","fish-oil supplement EPA1.7g+DHA1.2g within3.7g n-3",
                        "assigned 3.7g/day total n-3,6weeks",[EPA,DHA],amount=3.7,unit="g",days=42,
                        quantified_components={EPA:{"amount":1.7,"unit":"g/day"},DHA:{"amount":1.2,"unit":"g/day"}},
                        contrast_pattern="(?i)fishoil",dose_frequency="per_day")],
              [common,"11명·33개 end-period 배열; 서로 다른3개조직 SubSeries는 같은 대상자 연구.",
               "생선 한 끼와6주 보충제는 형태·기간이 다르다.","EPA와DHA의 독립 효과를 식별하지 않는다.",
               "fenofibrate는 약물 비교이며 음식 중재로 표시하지 않는다."],11,33),
    ]
    # Allocated groups or crossover conditions are distinct from exposure ingredients.
    arm_specs={
        "S01":[("placebo","Placebo visit",["meal"],5),("blueberry","Blueberry visit",["meal"],5),("dha","DHA visit",["meal","dha"],5)],
        "S02":[(str(k),f"{k} kcal challenge",[str(k)],14) for k in (500,1000,1500)],
        "S03":[("active_public","Public flavanol active-arm subset",["flavanols"],None)],
        "S04":[("evoo","MED + EVOO",["evoo"],12),("nuts","MED + mixed nuts",["nuts"],12),("lowfat","Low-fat advice",["lowfat"],12)],
        "S05":[("single","Single counselling intervention",["fiber"],18)],
        "N14":[("sfa","SFA shake condition",["sfa"],21),("pufa","PUFA shake condition",["pufa"],21)],
        "N15":[("yogurt","Probiotic yogurt condition",["yogurt_acute","yogurt_chronic"],7),("milk","Acidified milk condition",["milk_acute","milk_chronic"],7)],
        "N16":[("HDG","Healthy dietary guidance",[],88),("MED","Mediterranean diet",["walnuts"],81),("greenMED","Green Mediterranean diet",["walnuts","green"],87)],
        "N06":[("fishoil","Fish oil condition",["fish_oil"],11),("placebo","Placebo condition",[],11),("fenofibrate","Fenofibrate drug condition",[],11)]}
    for s in studies:
        s["arms"]=[{"key":key,"label":label,"exposure_keys":keys,"n_people":n,
            "role":"crossover_condition" if "crossover" in s["design"] else "study_group",
            "n_people_scope":"public_omics_subset; not total parent clinical trial"}
            for key,label,keys,n in arm_specs[s["id"]]]
    dairy=next(s for s in studies if s["id"]=="N15")
    dairy["clinical_endpoints"]=[
        {"id":"N15:clinical:insulin_iauc", "endpoint":"insulin incremental AUC",
         "contrast":"yogurt versus acidified milk; 800g acute crossover",
         "contrast_pattern":r"[246]h_change", "finding":"greater response after yogurt",
         "statistical_status":"reported_significant", "effect_estimate":None,
         "effect_unit":None,"p_value":None,"q_value":None,"n_people":13,
         "value_status":"qualitative", "uncertainty_status":"numeric summary not transcribed",
         "source_url":dairy["sources"][0]["url"],
         "source_location":"Results: Postprandial responses to dairy intake; supplementary S3 Table",
         "source_table_url":"https://doi.org/10.1371/journal.pone.0192947.s008",
         "review_status":"source_checked", "expert_review_status":"pending",
         "health_benefit_label":None, "paired_to_public_rna_individuals":False,
         "export_allowed":True},
        *[{"id":"N15:clinical:"+key,"endpoint":label,
           "contrast":"yogurt versus acidified milk; 800g acute crossover",
           "contrast_pattern":r"[246]h_change", "finding":"no significant between-product difference reported",
           "statistical_status":"reported_nonsignificant", "effect_estimate":None,
           "effect_unit":None,"p_value":p,"q_value":None,"n_people":13,
           "value_status":"qualitative", "uncertainty_status":"not an equivalence test; numeric effect not transcribed",
           "source_url":dairy["sources"][0]["url"],
           "source_location":"Results: Postprandial responses to dairy intake; supplementary S3 Table",
           "review_status":"source_checked", "expert_review_status":"pending",
           "health_benefit_label":None,"paired_to_public_rna_individuals":False,"export_allowed":True}
          for key,label,p in [("glucose","glucose incremental AUC",None),
                              ("lipids","triglycerides and cholesterol fractions",None),
                              ("tnfa","TNF-alpha incremental AUC",0.10)]]
    ]
    return {"version":V,"created_at":"2026-10-01",
            "curation_revision":"2026-10-20-primary-reference-v2",
            "revision_note":"Stable bridge identifiers are retained; the N06 primary-publication link is corrected. Frozen earlier manifests are separate inputs.",
            "expert_review_status":"pending",
            "scope":"source-backed noncommercial human intervention evidence retrieval","studies":studies}


if __name__=="__main__":
    out=Path(__file__).resolve().parents[1]/"config"/"human_exposures.v1.json"
    out.write_text(json.dumps(manifest(),ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(out)
