import pathlib,json,csv,shutil,hashlib,sys,platform,os
from datetime import datetime,timezone
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch,FancyArrowPatch

here=pathlib.Path(__file__).resolve().parent;portable=(here/'structure-source.json').exists();dest=here if portable else here/'보고서_구조도';dest.mkdir(exist_ok=True)
source=here/'structure-source.json' if portable else here/'public-base-metadata.json'
if not portable:shutil.copyfile(source,dest/'structure-source.json')
for name in ['malgun.ttf','malgunbd.ttf']:
 p=pathlib.Path('C:/Windows/Fonts')/name
 if p.exists():font_manager.fontManager.addfont(str(p))
custom_font=os.environ.get('NUTRIOMICS_FIGURE_FONT')
if custom_font:
 font_manager.fontManager.addfont(custom_font)
 preferred_font=font_manager.FontProperties(fname=custom_font).get_name()
else:
 installed={entry.name for entry in font_manager.fontManager.ttflist}
 preferred_font=next((name for name in ['Malgun Gothic','Noto Sans CJK KR','Noto Sans CJK JP','NanumGothic'] if name in installed),None)
 if preferred_font is None:raise RuntimeError('A Korean font is required. Install Noto Sans CJK or set NUTRIOMICS_FIGURE_FONT to a font file.')
plt.rcParams.update({'font.family':[preferred_font,'DejaVu Sans'],'font.size':14,'svg.fonttype':'none','svg.hashsalt':'nutriomics-schema-ko-20261024','pdf.fonttype':42})
nodes=[('food','음식·섭취량','node(food), API items.food_id/amount_g'),('component','영양성분·화합물','measurement, node(component)'),('intervention','사람 식이중재','science_record(study/exposure/arm/contrast)'),('omics','발현·메틸화·경로','study_effect, Parquet, five-part analysis key'),('health','같은 연구의 건강지표','science_record(clinical_endpoint_result), node(outcome)'),('context','기전·계산 예측','edge with source/tier/taxon/tissue')]
edges=[('food','component','섭취량에 따른 성분량 계산','confirmed composition basis'),('component','intervention','관련 성분·실험 조건 대응','match/mismatch/unknown; no automatic causal transfer'),('food','intervention','음식·제품 이름으로도 조회','same record or related-food correspondence'),('intervention','omics','해당 대비의 관찰 결과 조회','study and five-part result key'),('intervention','health','같은 연구 ID로 함께 조회','co-context, not mediation'),('component','context','관련 근거가 있을 때 참고','optional, source-specific mechanism or prediction'),('context','omics','유전자·조절 영역의 해석 참고','separate from dietary intervention evidence')]
for name,header,rows in [('nodes.csv',['id','label','storage'],nodes),('edges.csv',['source','target','meaning','scope'],edges)]:
 with (dest/name).open('w',encoding='utf-8-sig',newline='') as f:w=csv.writer(f);w.writerow(header);w.writerows(rows)
fig=plt.figure(figsize=(12,6.9));ax=fig.add_axes([.02,.06,.96,.86]);ax.set(xlim=(0,1),ylim=(0,1));ax.axis('off');dark='#173b50';line='#69869a'
fig.text(.04,.955,'음식에서 사람 연구의 관찰 결과까지',fontsize=23,weight='bold',color=dark)
fig.text(.04,.91,'성분량을 계산하고, 실제 실험의 조건과 결과를 함께 확인하는 구조',fontsize=14,color='#536c7b')
def box(x,y,w,h,title,body,fill='#f0f5f8',border='#a5bbc9',dash=False):
 ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=.008,rounding_size=.012',facecolor=fill,edgecolor=border,lw=1.2,linestyle='--' if dash else '-'))
 ax.text(x+w/2,y+h*.78,title,ha='center',va='center',fontsize=17,weight='bold',color=dark)
 ax.text(x+w/2,y+h*.36,body,ha='center',va='center',fontsize=14,linespacing=1.55,color='#304e60')
def arrow(start,end,dashed=False,curve=0):
 ax.add_patch(FancyArrowPatch(start,end,arrowstyle='-|>',mutation_scale=15,lw=1.6,color=line,linestyle='--' if dashed else '-',connectionstyle=f'arc3,rad={curve}'))
xs=[.02,.275,.53,.785];w=.19;y=.415;h=.255
box(xs[0],y,w,h,'음식·섭취량','음식명 · 조리 상태\n입력한 섭취량(g)')
box(xs[1],y,w,h,'영양성분·화합물','성분량 · 단위 · 기준\n사용한 성분표의 출처')
box(xs[2],y,w,h,'사람 식이중재','음식·제품·성분\n용량·기간·조직\n대조 조건')
box(xs[3],y,w,h,'오믹스 관찰 결과','유전자 발현\nDNA 메틸화 · 경로')
for i,text in enumerate(['함량 계산','관련 연구 찾기','대비·버전으로 조회']):
 arrow((xs[i]+w+.01,y+h/2),(xs[i+1]-.01,y+h/2));ax.text((xs[i]+w+xs[i+1])/2,y-.035,text,ha='center',va='top',fontsize=11,color='#536c7b',rotation=0)
box(.625,.77,.35,.145,'기전·계산 예측 (별도 층)','종 · 조직 · 근거 수준을 구분',fill='#f8f8f8',border='#aab5bd',dash=True)
arrow((.37,.683),(.612,.832),True,curve=-.10);ax.text(.47,.78,'관련 근거가 있을 때',ha='center',fontsize=11.5,color='#536c7b',rotation=16)
arrow((.88,.756),(.88,.683),True)
box(.53,.085,.445,.205,'같은 연구의 건강지표','혈압 · 지질 · BMI · 허리둘레 등\n수록된 경우 연구 조건과 함께 확인',fill='#f2f6fa')
arrow((.625,.402),(.625,.307))
ax.text(.02,.29,'음식·제품 이름으로도 연구를 찾을 수 있습니다.\n조건의 일치·불일치·미확인을 각각 표시하며,\n연결 근거가 없는 단계는 자료 부족으로 남깁니다.',fontsize=13,color='#536c7b',va='top',linespacing=1.8)
fig.text(.04,.025,'실선: 조성·연구 기록에 따른 조회 연결   |   파선: 별도 기전·예측 맥락   |   화살표는 인과성이나 건강상 이득을 뜻하지 않음',fontsize=11,color='#536c7b')
for ext in ['png','pdf','svg']:
 meta={'Creator':'NutriOmics report figure'}
 if ext=='pdf':meta.update(CreationDate=datetime(2026,10,24,tzinfo=timezone.utc),ModDate=datetime(2026,10,24,tzinfo=timezone.utc))
 elif ext=='svg':meta['Date']='2026-10-24'
 fig.savefig(dest/('보고서용_DB_구조도.'+ext),dpi=300,metadata=meta)
plt.close(fig)
(dest/'legend.txt').write_text('그림. NutriOmics DB의 구성과 자료 연결. 음식과 섭취량에서 확인된 함량 기준에 따라 성분량을 계산하고, 관련 사람 식이중재의 노출·용량·기간·조직·대조 조건과 연결한다. 유전자 발현, DNA 메틸화 및 경로 결과는 해당 연구·대비·분석 버전으로 조회한다. 혈압·지질·비만 관련 건강지표는 같은 연구의 별도 관찰 결과로 함께 제공한다. 기전 가설과 계산 예측은 별도 계층으로 표시한다. 선은 자료 조회의 관계이며, 음식의 효능이나 유전체 변화의 임상적 인과성을 뜻하지 않는다. 조건이나 연결이 확인되지 않은 항목은 미확인·자료 부족으로 유지한다. 그림의 화살표는 모든 식품에서 각 단계의 연결이 완성되었다는 뜻이 아니다.\n',encoding='utf-8')
(dest/'provenance.json').write_text(json.dumps(dict(regeneration='python make_korean_structure.py',input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),schema_sources=['node','measurement','science_record','study_effect','edge'],new_effect_estimates_generated=False,causal_pathways_inferred=False,source_tables=['nodes.csv','edges.csv'],font=preferred_font,python_version=sys.version,matplotlib_version=matplotlib.__version__,platform=platform.platform(),scientific_approval=False,limitations=['Descriptive data joins, not causal mechanisms','Not every food has a complete path','Scientific inclusion review remains pending']),ensure_ascii=False,indent=2),encoding='utf-8')
if not portable:shutil.copyfile(pathlib.Path(__file__),dest/'make_korean_structure.py')
print(str(dest))
