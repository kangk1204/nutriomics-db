script<-sub("^--file=","",grep("^--file=",commandArgs(),value=TRUE)[1]); source(file.path(dirname(script),"bridge_omics_helpers.R")); suppressPackageStartupMessages(library(edgeR))
root<-arg_value("--root",Sys.getenv("NUTRIOMICS_ANALYSIS_ROOT", unset="expansion"))
raw<-file.path(root,"raw","GSE98645"); output<-arg_value("--output-dir",file.path(root,"outputs","gse98645-acute-v1"));
dir.create(output,recursive=TRUE,showWarnings=FALSE)
records<-fromJSON(file.path(raw,"sample_metadata.json"),simplifyVector=FALSE)
sample_info<-do.call(rbind,lapply(records,function(x) {
  link<-unlist(x$supplementary_file_1); label<-sub("^.*GSM[0-9]+_([A-J][0-9]+)\\.counts\\.txt\\.gz$","\\1",link)
  title<-x$title[[1]]; minute<-as.integer(sub("^.*_T([0-9]+)$","\\1",title))
  if(length(label)!=1 || !grepl("^[A-J][0-9]+$",label) || is.na(minute)) stop("Count-to-GSM mapping unresolved")
  data.frame(sample_accession=x$sample_accession,count_label=label,subject_id=get_characteristic(x,"subject id"),
    condition=get_characteristic(x,"condition"),visit=sub("^.*_(V[0-9]+)_T.*$","\\1",title),time_hours=minute/60,
    source_time_text=get_characteristic(x,"time point \\(h\\)"),
    time_audit="T120/T240/T360 title values are minutes per published protocol; GEO characteristic h suffix is erroneous")
}))
if(nrow(sample_info)!=77L || length(unique(sample_info$subject_id))!=7L || anyDuplicated(sample_info$count_label)) stop("Unexpected RNA subset")
counts<-read.delim(gzfile(file.path(raw,"GSE98645_combined.counts.txt.gz")),check.names=FALSE,row.names=1)
counts<-as.matrix(counts); storage.mode(counts)<-"numeric"
if(!setequal(colnames(counts),sample_info$count_label)||any(!is.finite(counts))||any(counts<0)||any(counts!=floor(counts))) stop("Invalid count matrix")
counts<-counts[,sample_info$count_label,drop=FALSE]; colnames(counts)<-sample_info$sample_accession
write.table(sample_info,file.path(output,"all_sample_design.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
all_sample_info<-sample_info; all_counts<-counts
acute<-grepl("^Acute_",sample_info$condition); sample_info<-sample_info[acute,]; counts<-counts[,acute,drop=FALSE]
sample_info$arm<-sub("^Acute_","",sample_info$condition)
if(nrow(sample_info)!=56L || any(table(sample_info$subject_id,sample_info$arm,sample_info$time_hours)!=1L)) stop("Incomplete person-arm-time acute cells")
sample_info$subject_id<-factor(sample_info$subject_id);sample_info$visit<-factor(sample_info$visit)
sample_info$condition<-factor(paste0(sample_info$arm,"_",sample_info$time_hours,"h"))
design<-model.matrix(~0+condition+subject_id+visit,sample_info);colnames(design)<-sub("^condition","",colnames(design))
contrasts<-makeContrasts(yogurt_vs_milk_2h_change=(Yogurt_2h-Yogurt_0h)-(Milk_2h-Milk_0h),
 yogurt_vs_milk_4h_change=(Yogurt_4h-Yogurt_0h)-(Milk_4h-Milk_0h),
 yogurt_vs_milk_6h_change=(Yogurt_6h-Yogurt_0h)-(Milk_6h-Milk_0h),levels=design)
y<-DGEList(counts); keep<-filterByExpr(y,design=design);y<-calcNormFactors(y[keep,,keep.lib.sizes=FALSE],method="TMM")
v<-voom(y,design,plot=FALSE);expression<-v$E
entrez<-unname(mapIds(org.Hs.eg.db,keys=rownames(expression),column="ENTREZID",keytype="ENSEMBL",multiVals="first"))
labels<-unname(mapIds(org.Hs.eg.db,keys=rownames(expression),column="SYMBOL",keytype="ENSEMBL",multiVals="first"));labels[is.na(labels)]<-rownames(expression)[is.na(labels)]
export_expression(expression,sample_info,design,contrasts,"GSE98645","gse98645-voom-acute-paired-v1",output,entrez,labels,
 context=list(tissue="whole blood",timepoint=list("2h change difference","4h change difference","6h change difference"),
 dose=as.list(rep("800 g single challenge",3)),duration=as.list(rep("acute",3)),intervention="Probiotic yogurt versus acidified non-fermented milk",
 normalization="Unfiltered public counts; edgeR filterByExpr/TMM; limma-voom; person and test-visit fixed effects",
 interpretation="Seven independent RNA-seq people; 56 acute samples. Clinical 13-person subset is separate. Product comparison cannot isolate probiotic, fermentation, or chemical effects. No personal health prediction."),
 input_files=c(file.path(raw,"GSE98645_combined.counts.txt.gz"),file.path(raw,"sample_metadata.json")),
 qc_limit="Count-level QC; sequencing alignment/read QC not independently reproduced from FASTQ. Chronic 21 samples preserved in all_sample_design but excluded from this acute analysis.",voom_object=v)

# Each two-week endpoint maps to its explicit preceding acute-test baseline by
# person and product; the initial run-in sample is not reused as both baselines.
chronic_keep<-all_sample_info$condition %in% c("After_2wks_Milk","After_2wks_Yogurt") |
 (grepl("^Acute_",all_sample_info$condition)&all_sample_info$time_hours==0)
sample_info<-all_sample_info[chronic_keep,]; counts<-all_counts[,chronic_keep,drop=FALSE]
sample_info$arm<-sub("^(Acute_|After_2wks_)","",sample_info$condition)
sample_info$endpoint<-ifelse(grepl("^Acute_",sample_info$condition),"baseline","2w")
sample_info$period<-ifelse(sample_info$visit %in% c("V2","V3"),"first","second")
if(nrow(sample_info)!=28L||any(table(sample_info$subject_id,sample_info$arm,sample_info$endpoint)!=1L)) stop("Chronic before-after cells incomplete")
for(person in unique(sample_info$subject_id)) for(arm in unique(sample_info$arm)) {
 rows<-sample_info$subject_id==person&sample_info$arm==arm
 if(length(unique(sample_info$period[rows]))!=1L) stop("Post-period product disagrees with preceding baseline product")
}
sample_info$subject_id<-factor(sample_info$subject_id);sample_info$period<-factor(sample_info$period)
sample_info$condition<-factor(paste(sample_info$arm,sample_info$endpoint,sep="_"))
design<-model.matrix(~0+condition+subject_id+period,sample_info);colnames(design)<-sub("^condition","",colnames(design))
contrasts<-makeContrasts(yogurt_vs_milk_2week_change=(Yogurt_2w-Yogurt_baseline)-(Milk_2w-Milk_baseline),levels=design)
y<-DGEList(counts);keep<-filterByExpr(y,design=design);y<-calcNormFactors(y[keep,,keep.lib.sizes=FALSE],method="TMM");v<-voom(y,design,plot=FALSE);expression<-v$E
entrez<-unname(mapIds(org.Hs.eg.db,keys=rownames(expression),column="ENTREZID",keytype="ENSEMBL",multiVals="first"))
labels<-unname(mapIds(org.Hs.eg.db,keys=rownames(expression),column="SYMBOL",keytype="ENSEMBL",multiVals="first"));labels[is.na(labels)]<-rownames(expression)[is.na(labels)]
output<-file.path(root,"outputs","gse98645-chronic-v1");dir.create(output,recursive=TRUE,showWarnings=FALSE)
export_expression(expression,sample_info,design,contrasts,"GSE98645","gse98645-voom-chronic-paired-v1",output,entrez,labels,
 context=list(tissue="whole blood",timepoint=list("2-week change difference"),dose=list("400 g/day, 2 weeks"),duration=list("2 weeks"),
 intervention="Probiotic yogurt versus acidified non-fermented milk",normalization="Unfiltered counts; edgeR filterByExpr/TMM; limma-voom; person and treatment-period fixed effects",
 interpretation="Seven people with paired product-specific before/after visits; washout/product sequence from explicit GEO condition labels. Cannot isolate probiotic or chemical causal effects."),
 input_files=c(file.path(raw,"GSE98645_combined.counts.txt.gz"),file.path(raw,"sample_metadata.json")),
 qc_limit="Count-level QC only; FASTQ not independently reprocessed.",voom_object=v)
