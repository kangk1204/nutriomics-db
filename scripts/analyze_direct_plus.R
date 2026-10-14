# Public DIRECT PLUS IDAT reanalysis, with resumable sample-normalization chunks.
# Estimand: randomized diet-arm difference in paired 18-month methylation change.
script<-sub("^--file=","",grep("^--file=",commandArgs(),value=TRUE)[1])
suppressPackageStartupMessages({library(minfi);library(limma);library(jsonlite)})
args<-commandArgs(trailingOnly=TRUE)
arg_value<-function(name,default){k<-match(name,args);if(is.na(k)||k==length(args))default else args[[k+1L]]}
root<-arg_value("--root",Sys.getenv("NUTRIOMICS_ANALYSIS_ROOT", unset="expansion"))
output<-arg_value("--output-dir",file.path(root,"outputs","direct-plus-idat-v1"));dir.create(output,recursive=TRUE,showWarnings=FALSE)
raw<-file.path(root,"raw","E-MTAB-12527");chunk_root<-file.path(output,"normalization_chunks");dir.create(chunk_root,showWarnings=FALSE)
file_sha<-function(path) strsplit(system2("sha256sum",shQuote(path),stdout=TRUE)," +")[[1L]][1L]
write_gzip_tsv<-function(x,p){con<-gzfile(p,"wt");on.exit(close(con));write.table(x,con,sep="\t",quote=FALSE,row.names=FALSE,na="NA")}
sdrf_path<-file.path(raw,"E-MTAB-12527.sdrf.txt")
sdrf<-read.delim(sdrf_path,check.names=FALSE,stringsAsFactors=FALSE)
green<-grepl("_Grn\\.idat$",sdrf[["Array Data File"]]); samples<-sdrf[green,]
sample_info<-data.frame(sample_id=samples[["Source Name"]],subject_id=samples[["Characteristics[individual]"]],
 arm=samples[["Factor Value[diet]"]],time_months=as.integer(samples[["Factor Value[time]"]]),
 sex=samples[["Characteristics[sex]"]],source_age=samples[["Characteristics[age]"]],
 sentrix_id=samples[["Comment[Array_Sentrix_ID]"]],position=samples[["Comment[Array_Sentrix_Position]"]],
 plate=samples[["Comment[Array_Sample_Plate]"]],base=sub("_Grn\\.idat$","",samples[["Array Data File"]]),stringsAsFactors=FALSE)
sample_info<-sample_info[order(sample_info$base),]
if(nrow(sample_info)!=512L||length(unique(sample_info$subject_id))!=256L||anyDuplicated(paste(sample_info$subject_id,sample_info$time_months)))stop("Public pair structure changed")
if(any(table(sample_info$subject_id,sample_info$time_months)!=1L))stop("Complete 0/18 month pairs required")
if(any(vapply(split(sample_info$arm,sample_info$subject_id),function(x)length(unique(x)),integer(1))!=1L))stop("Diet differs within individual")
for(i in seq_len(nrow(sample_info))) {
 channels<-sdrf[sdrf[["Source Name"]]==sample_info$sample_id[i],"Array Data File"]
 if(length(channels)!=2L||!setequal(channels,paste0(sample_info$base[i],c("_Grn.idat","_Red.idat"))))stop("IDAT channel mapping ambiguous")
}
write.table(sample_info,file.path(output,"source_sample_metadata.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
write.table(as.data.frame(table(sample_info$arm,sample_info$time_months,sample_info$plate)),
 file.path(output,"technical_batch_balance.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
listing<-fromJSON(file.path(raw,"raw-data_filelist.json")); expected<-setNames(as.numeric(listing$size),listing$path)
chunk_size<-as.integer(arg_value("--chunk-size","32"))
groups<-split(seq_len(nrow(sample_info)),ceiling(seq_len(nrow(sample_info))/chunk_size))
legacy_key<-paste(file_sha(sdrf_path),as.character(packageVersion("minfi")),chunk_size,"ssNoob-single-dye-v1",sep="|")
identity<-list(normalization="read.metharray.exp extended=FALSE; detectionP .01; preprocessNoob dyeCorr=TRUE,dyeMethod=single; mapToGenome; getBeta/getM",
 minfi=as.character(packageVersion("minfi")),annotation=as.character(packageVersion("IlluminaHumanMethylationEPICanno.ilm10b4.hg19")),
 manifest=as.character(packageVersion("IlluminaHumanMethylationEPICmanifest")))
identity_path<-file.path(output,"normalization_identity.json");write_json(identity,identity_path,auto_unbox=TRUE,pretty=TRUE)
checkpoint_key<-paste(legacy_key,file_sha(identity_path),sep="|")
legacy_script<-file.path(output,"source_at_launch","analyze_direct_plus.R")
legacy_source_verified<-file.exists(legacy_script)&&file_sha(legacy_script)=="4d24cb2533bc94fd1f9b43b5439ff83c8e73b0ef9135e4e22daafc13814a1e36"
for(k in seq_along(groups)) {
 indices<-groups[[k]];chunk_path<-file.path(chunk_root,sprintf("chunk_%03d.rds",k))
 if(file.exists(chunk_path)) {
  old<-readRDS(chunk_path)
  if((identical(old$checkpoint_key,checkpoint_key)||(identical(old$checkpoint_key,legacy_key)&&legacy_source_verified))&&identical(old$sample_ids,sample_info$sample_id[indices])) {
   if(is.null(old$idat_manifest)||any(vapply(old$idat_manifest,function(x)file_sha(x$path),character(1))!=vapply(old$idat_manifest,function(x)x$sha256,character(1))))stop("Checkpoint IDAT hash changed")
   write_json(list(chunk=k,original_checkpoint_key=old$checkpoint_key,normalization_identity_sha256=file_sha(identity_path),
     legacy_executed_source_sha256=if(identical(old$checkpoint_key,legacy_key))file_sha(legacy_script) else NA_character_,
     legacy_checkpoint_limitation="Original keys lacked code and annotation identity; reuse only after frozen original source and unchanged normalization recipe verified"),
     file.path(chunk_root,sprintf("chunk_%03d_resume_audit.json",k)),auto_unbox=TRUE,pretty=TRUE)
   cat("RESUME chunk",k,"\n");rm(old);gc();next
  }
  stop("Checkpoint does not match frozen metadata/environment")
 }
 files<-as.vector(outer(sample_info$base[indices],c("_Grn.idat","_Red.idat"),paste0))
 deadline<-Sys.time()+as.integer(arg_value("--download-wait-seconds","1800"))
 repeat {
  paths<-file.path(raw,"idat",files); sizes<-file.info(paths)$size
  ready<-all(!is.na(sizes)&sizes==unname(expected[files]))
  if(ready)break
  if(Sys.time()>deadline)stop("Required IDAT batch incomplete; checkpoints retained. Resume after downloader completes.")
  Sys.sleep(5)
 }
 cat("NORMALIZE chunk",k,"of",length(groups),"samples",length(indices),"\n");flush.console()
 targets<-data.frame(Sample_Name=sample_info$sample_id[indices],Basename=file.path(raw,"idat",sample_info$base[indices]))
 rg<-read.metharray.exp(targets=targets,extended=FALSE,force=FALSE,verbose=TRUE)
 if(!identical(sampleNames(rg),basename(targets$Basename)))stop("minfi array order changed")
 sampleNames(rg)<-sample_info$sample_id[indices]
 det<-detectionP(rg);qc<-colMeans(det>.01,na.rm=TRUE)
 # Single-sample Noob keeps each chunk scientifically equivalent to per-sample
 # normalization. No reference sample is selected independently in each chunk.
 normalized<-mapToGenome(preprocessNoob(rg,dyeCorr=TRUE,dyeMethod="single",verbose=TRUE))
 beta<-getBeta(normalized);m_values<-getM(normalized)
 if(k==1L) {
  annotation<-as.data.frame(getAnnotation(normalized))
  saveRDS(annotation,file.path(output,"probe_annotation.rds"),compress=FALSE)
 }
 pass<-det[match(rownames(beta),rownames(det)),,drop=FALSE]<=.01
 result<-list(checkpoint_key=checkpoint_key,sample_ids=sample_info$sample_id[indices],beta=beta,m_values=m_values,detected=pass,sample_detection_fail_fraction=qc,
  idat_manifest=lapply(paths,function(p)list(path=normalizePath(p),sha256=file_sha(p),bytes=file.info(p)$size)))
 write_json(result$idat_manifest,file.path(chunk_root,sprintf("chunk_%03d_idat_manifest.json",k)),auto_unbox=TRUE,pretty=TRUE)
 temporary<-paste0(chunk_path,".tmp");saveRDS(result,temporary,compress=FALSE);file.rename(temporary,chunk_path)
 rm(rg,det,normalized,beta,m_values,pass,result);gc()
}
chunks<-lapply(seq_along(groups),function(k)readRDS(file.path(chunk_root,sprintf("chunk_%03d.rds",k))))
sample_info$detection_failure_fraction<-unlist(lapply(chunks,function(x)x$sample_detection_fail_fraction),use.names=FALSE)
bad_people<-unique(sample_info$subject_id[sample_info$detection_failure_fraction>.05|!is.finite(sample_info$detection_failure_fraction)])
sample_info$included<-!sample_info$subject_id %in% bad_people
sample_info$inclusion_reason<-ifelse(sample_info$included,"Complete pair; each sample detection failure <=5%","At least one paired sample failed detection QC; both excluded")
detected_count<-Reduce(`+`,lapply(seq_along(chunks),function(k)rowSums(chunks[[k]]$detected[,sample_info$included[groups[[k]]],drop=FALSE],na.rm=TRUE)))
probe_keep<-detected_count>=.95*sum(sample_info$included)
full_probe_audit<-data.frame(feature_id=rownames(chunks[[1]]$beta),detection_pass=probe_keep,
 detection_pass_count=detected_count,included_assays=sum(sample_info$included),stringsAsFactors=FALSE)
beta<-do.call(cbind,lapply(chunks,function(x)x$beta[probe_keep,,drop=FALSE]))
m_values<-do.call(cbind,lapply(chunks,function(x)x$m_values[probe_keep,,drop=FALSE]))
rm(chunks);gc()
annotation<-readRDS(file.path(output,"probe_annotation.rds"));annotation<-annotation[match(rownames(beta),rownames(annotation)),]
# Remove manufacturer-annotated SNP-affected CpGs and sex chromosomes from the
# primary analysis; all exclusion categories are retained in the probe audit.
snp_bad<-rep(FALSE,nrow(annotation))
for(col in intersect(c("CpG_rs","SBE_rs"),names(annotation)))snp_bad<-snp_bad|(!is.na(annotation[[col]])&nzchar(as.character(annotation[[col]])))
autosomal<-!as.character(annotation$chr)%in%c("chrX","chrY","X","Y")
finite<-rowSums(!is.finite(m_values[,sample_info$included,drop=FALSE]))==0L & rowSums(!is.finite(beta[,sample_info$included,drop=FALSE]))==0L
is_cpg<-grepl("^cg[0-9]+$",rownames(beta))
selected<-!snp_bad&autosomal&finite&is_cpg
full_probe_audit$included<-FALSE;full_probe_audit$exclusion_reason<-"Detection p>.01 in more than 5% of included assays"
indices<-match(rownames(beta),full_probe_audit$feature_id)
full_probe_audit$included[indices]<-selected
full_probe_audit$exclusion_reason[indices]<-ifelse(!is_cpg,"Non-CpG identifier",ifelse(snp_bad,"Manufacturer SNP-affected CpG/SBE annotation",ifelse(!autosomal,"Sex chromosome",ifelse(!finite,"Nonfinite value in included assay","Included CpG"))))
write.table(full_probe_audit,file.path(output,"probe_inclusion.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
beta<-beta[selected,,drop=FALSE];m_values<-m_values[selected,,drop=FALSE];annotation<-annotation[selected,]
people<-unique(sample_info$subject_id[sample_info$included])
baseline<-match(paste(people,0),paste(sample_info$subject_id,sample_info$time_months))
followup<-match(paste(people,18),paste(sample_info$subject_id,sample_info$time_months))
delta_m<-m_values[,followup,drop=FALSE]-m_values[,baseline,drop=FALSE]
delta_beta<-beta[,followup,drop=FALSE]-beta[,baseline,drop=FALSE]
person_info<-sample_info[baseline,];person_info$arm<-factor(person_info$arm,levels=c("HDG","MED","greenMED"))
design<-model.matrix(~0+arm,person_info);colnames(design)<-levels(person_info$arm)
contrasts<-makeContrasts(greenMED_vs_MED_18month_change=greenMED-MED,MED_vs_HDG_18month_change=MED-HDG,greenMED_vs_HDG_18month_change=greenMED-HDG,levels=design)
plate_table<-table(sample_info$arm[sample_info$included],sample_info$time_months[sample_info$included],sample_info$plate[sample_info$included])
person_info$baseline_plate<-factor(person_info$plate)
plate_design<-model.matrix(~0+arm+baseline_plate,person_info)
plate_full_rank<-qr(plate_design)$rank==ncol(plate_design)
write_json(list(status=if(plate_full_rank)"passed_with_declared_limitations" else "failed",
 included_people=length(people),arm_by_time_by_plate_zero_cells=sum(plate_table==0),
 sensitivity_design_rank=qr(plate_design)$rank,sensitivity_design_columns=ncol(plate_design),
 sensitivity_residual_df=nrow(plate_design)-qr(plate_design)$rank,
 interpretation="All arm/time/plate counts preserved; arm contrast estimable with baseline plate nuisance effects. Primary is unadjusted total response; no perfect confounding does not rule out technical bias."),
 file.path(output,"technical_identifiability.json"),auto_unbox=TRUE,pretty=TRUE)
if(!plate_full_rank)stop("Arm contrast not identifiable after technical plate nuisance model; no unreviewed primary release")
fit_m<-eBayes(contrasts.fit(lmFit(delta_m,design),contrasts),robust=TRUE)
fit_beta<-eBayes(contrasts.fit(lmFit(delta_beta,design),contrasts),robust=TRUE)
gene_labels<-if("UCSC_RefGene_Name"%in%names(annotation))as.character(annotation$UCSC_RefGene_Name) else rep(NA_character_,nrow(annotation))
tables<-lapply(seq_len(ncol(contrasts)),function(i){
 se<-fit_beta$stdev.unscaled[,i]*sqrt(fit_beta$s2.post);critical<-qt(.975,fit_beta$df.total)
 arms<-rownames(contrasts)[contrasts[,i]!=0];n<-sum(person_info$arm%in%arms)
 data.frame(feature_type="cpg",feature_id=rownames(delta_m),feature_label=ifelse(is.na(gene_labels)|!nzchar(gene_labels),rownames(delta_m),gene_labels),
 contrast=colnames(contrasts)[i],tissue="whole blood",timepoint="18-month change difference",intervention="DIRECT PLUS randomized diet-pattern comparison",
 dose=NA_character_,duration="18 months",effect_estimate=fit_beta$coefficients[,i],standard_error=se,
 confidence_low=fit_beta$coefficients[,i]-critical*se,confidence_high=fit_beta$coefficients[,i]+critical*se,
 p_value=fit_m$p.value[,i],q_value=p.adjust(fit_m$p.value[,i],"BH"),effect_unit="delta delta beta",status="measured",n_people=n,n_samples=sum(sample_info$included),contrast_assay_count=2*n,
 m_value_estimate=fit_m$coefficients[,i],m_value_standard_error=fit_m$stdev.unscaled[,i]*sqrt(fit_m$s2.post),
 chromosome=as.character(annotation$chr),position=as.character(annotation$pos),genome_build="hg19",annotation_gene=gene_labels,
 cell_composition_adjusted=FALSE,stringsAsFactors=FALSE)
})
results<-do.call(rbind,tables);results$q_value_contrast<-results$q_value;results$q_value_study_family<-p.adjust(results$p_value,"BH");results$q_value<-results$q_value_study_family
write_gzip_tsv(results,file.path(output,"cpg_effects.tsv.gz"))
write.table(sample_info,file.path(output,"sample_metadata.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
write.table(sample_info[,c("sample_id","subject_id","arm","time_months","detection_failure_fraction","included","inclusion_reason")],file.path(output,"sample_qc.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
saveRDS(list(delta_m=delta_m,delta_beta=delta_beta,person_info=person_info,design=design,contrasts=contrasts),file.path(output,"paired_change_inputs.rds"),compress=FALSE)
metadata<-list(accession="E-MTAB-12527",analysis_version="direct-plus-ssnoob-limma-between-arm-v2",run_utc=format(Sys.time(),tz="UTC",usetz=TRUE),
 script_path=normalizePath(script),script_sha256=file_sha(script),independent_people=length(people),sample_count=sum(sample_info$included),
 source_people=256,source_samples=512,public_paper_discrepancy="Paper reports 260 DNAm participants; public deposit has 256 complete pairs; no missing participants invented",
 result_files=c("cpg_effects.tsv.gz"),auxiliary_files=c("sample_metadata.tsv","sample_qc.tsv","probe_inclusion.tsv","source_sample_metadata.tsv","technical_batch_balance.tsv","technical_identifiability.json","normalization_identity.json"),
 input_manifest=lapply(c(sdrf_path,file.path(raw,"study_metadata.json"),file.path(raw,"raw-data_filelist.json"),list.files(chunk_root,pattern="_idat_manifest[.]json$",full.names=TRUE)),function(p)list(path=normalizePath(p),sha256=file_sha(p),bytes=file.info(p)$size)),
 normalization="minfi single-sample Noob, dyeMethod=single; paired 18-month minus baseline M and beta matrices; arm fixed effects",
 multiple_testing="Primary BH across all CpGs times 3 primary between-arm contrasts; per-contrast BH retained",
 beta_uncertainty="Separate beta-change limma fit; M-change p-values and moderated beta-scale SE/95% CI",
 cell_composition_adjusted=FALSE,dmr_analysis="Not performed in this CpG run; nearest-gene annotations are not causal regulation",
 technical_batch_adjusted=FALSE,technical_batch_audit="Arm-by-time-by-plate counts and formal rank gate exported; plate-nuisance arm contrast estimable; no blind batch removal",
 pathway_analysis="Pending separate missMethyl probe-bias-corrected analysis; no naive nearest-gene enrichment",
 RNA_availability="Public individual RNA-seq/count data not established; no paired RNA-DNAm claims",
 interpretation_limit="Whole-blood total response; no individual CpG/health prediction. Randomized full dietary patterns cannot isolate walnut, tea, or polyphenol component effects. Public subset selection requires publication audit.",
 package_versions=list(R=as.character(getRversion()),minfi=as.character(packageVersion("minfi")),limma=as.character(packageVersion("limma"))))
write_json(metadata,file.path(output,"analysis_metadata.json"),auto_unbox=TRUE,pretty=TRUE)
cat(toJSON(list(status="completed",people=length(people),samples=sum(sample_info$included),cpgs=nrow(delta_m),contrasts=3),auto_unbox=TRUE),"\n")
