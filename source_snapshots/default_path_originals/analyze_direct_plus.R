# Public DIRECT PLUS IDAT reanalysis, with resumable sample-normalization chunks.
# Estimand: randomized diet-arm difference in paired 18-month methylation change.
script<-sub("^--file=","",grep("^--file=",commandArgs(),value=TRUE)[1])
helper_path<-file.path(dirname(script),"direct_plus_helpers.R")
source(helper_path)
suppressPackageStartupMessages({library(minfi);library(limma);library(jsonlite)})
args<-commandArgs(trailingOnly=TRUE)
arg_value<-function(name,default){k<-match(name,args);if(is.na(k)||k==length(args))default else args[[k+1L]]}
analysis_version<-arg_value("--analysis-version","direct-plus-ssnoob-limma-between-arm-v4")
if(!grepl("^[A-Za-z0-9][A-Za-z0-9._-]+$",analysis_version)||nchar(analysis_version)>200L)stop("Invalid analysis version")
root<-arg_value("--root","/home/keunsoo/projects/nutrigenomics-db/expansion/2026-10-01")
output<-arg_value("--output-dir",file.path(root,"outputs","direct-plus-idat-v1"));dir.create(output,recursive=TRUE,showWarnings=FALSE)
raw<-file.path(root,"raw","E-MTAB-12527");chunk_root<-file.path(output,"normalization_chunks");dir.create(chunk_root,showWarnings=FALSE)
file_sha<-dp_file_sha
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
resolved_path<-file.path(raw,"resolved_files.json")
resolved<-fromJSON(resolved_path);listing<-resolved$files
listing<-listing[listing$section=="raw-data",]
if(nrow(listing)!=1024L||anyDuplicated(listing$name)||!setequal(listing$name,sdrf[["Array Data File"]]))stop("Resolved official listing and SDRF IDAT set disagree")
expected<-setNames(as.numeric(listing$size),listing$name)
if(any(!is.finite(expected)|expected<=0))stop("Official raw sizes missing")
acquisition<-fromJSON(file.path(raw,"acquisition_status.json"))
if(!identical(acquisition$status,"raw_complete_local_sha256_verified")||!identical(acquisition$source_identity,resolved$source_identity))stop("Source download is incomplete or identity changed")
receipt_path<-file.path(raw,"raw_receipts.json")
if(file_sha(receipt_path)!=acquisition$raw_receipts_sha256)stop("Raw receipt changed")
receipts<-fromJSON(receipt_path)
if(nrow(receipts)!=1024L||anyDuplicated(basename(receipts$path))||!setequal(basename(receipts$path),listing$name))stop("Raw receipt set incomplete")
expected_sha<-setNames(receipts$sha256,basename(receipts$path))
chunk_size<-as.integer(arg_value("--chunk-size","32"))
groups<-split(seq_len(nrow(sample_info)),ceiling(seq_len(nrow(sample_info))/chunk_size))
if(!is.finite(chunk_size)||chunk_size<1L||chunk_size>64L)stop("chunk-size must be 1..64")
environment_lock<-arg_value("--environment-lock","")
if(!nzchar(environment_lock)||!file.exists(environment_lock))stop("Provide --environment-lock for the exact project R lock file")
packages<-c("minfi","limma","jsonlite","statmod","IlluminaHumanMethylationEPICanno.ilm10b4.hg19","IlluminaHumanMethylationEPICmanifest")
versions<-setNames(lapply(packages,function(p)as.character(packageVersion(p))),packages)
annotation_files<-unlist(lapply(tail(packages,2L),function(p)list.files(system.file("data",package=p),full.names=TRUE,recursive=TRUE)))
if(!length(annotation_files))stop("Annotation/manifest data files unavailable")
identity<-list(normalization="read.metharray.exp extended=FALSE; detectionP .01; preprocessNoob dyeCorr=TRUE,dyeMethod=single; mapToGenome; getBeta/getM",
 R=as.character(getRversion()),package_versions=versions,chunk_size=chunk_size,analysis_version=analysis_version,
 script_sha256=file_sha(script),helper_sha256=file_sha(helper_path),sdrf_sha256=file_sha(sdrf_path),
 resolved_source_sha256=file_sha(resolved_path),raw_receipts_sha256=file_sha(receipt_path),
 environment_lock_sha256=file_sha(environment_lock),
 annotation_data=lapply(annotation_files,function(p)list(path=normalizePath(p),sha256=file_sha(p))))
identity_path<-file.path(output,"normalization_identity.json")
if(file.exists(identity_path)&&!identical(fromJSON(identity_path,simplifyVector=FALSE),identity))stop("Existing output normalization identity differs; use a NEW output directory")
if(!file.exists(identity_path))write_json(identity,identity_path,auto_unbox=TRUE,pretty=TRUE)
checkpoint_key<-file_sha(identity_path)
for(k in seq_along(groups)) {
 indices<-groups[[k]];chunk_path<-file.path(chunk_root,sprintf("chunk_%03d.rds",k))
 if(file.exists(chunk_path)) {
  old<-readRDS(chunk_path)
  if(identical(old$checkpoint_key,checkpoint_key)&&identical(old$sample_ids,sample_info$sample_id[indices])) {
   if(is.null(old$idat_manifest)||any(vapply(old$idat_manifest,function(x)file_sha(x$path),character(1))!=vapply(old$idat_manifest,function(x)x$sha256,character(1))))stop("Checkpoint IDAT hash changed")
   if(is.null(old$annotation_sha256)||file_sha(file.path(output,"probe_annotation.rds"))!=old$annotation_sha256)stop("Checkpoint annotation changed")
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
 source_manifest<-lapply(paths,function(p)list(path=normalizePath(p),sha256=file_sha(p),bytes=file.info(p)$size))
 if(any(vapply(source_manifest,function(x)x$sha256,character(1))!=expected_sha[basename(paths)]))stop("Raw IDAT differs from source acquisition hash")
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
  dp_atomic_rds(annotation,file.path(output,"probe_annotation.rds"))
 }
 pass<-det[match(rownames(beta),rownames(det)),,drop=FALSE]<=.01
 result<-list(checkpoint_key=checkpoint_key,sample_ids=sample_info$sample_id[indices],beta=beta,m_values=m_values,detected=pass,sample_detection_fail_fraction=qc,
  annotation_sha256=file_sha(file.path(output,"probe_annotation.rds")),idat_manifest=source_manifest)
 if(any(vapply(paths,file_sha,character(1))!=vapply(source_manifest,function(x)x$sha256,character(1))))stop("IDAT changed during normalization")
 write_json(result$idat_manifest,file.path(chunk_root,sprintf("chunk_%03d_idat_manifest.json",k)),auto_unbox=TRUE,pretty=TRUE)
 temporary<-paste0(chunk_path,".tmp");saveRDS(result,temporary,compress=FALSE);file.rename(temporary,chunk_path)
 rm(rg,det,normalized,beta,m_values,pass,result);gc()
}
# Two bounded-memory QC passes. At most ONE sample-normalization chunk is held.
chunk_paths<-file.path(chunk_root,sprintf("chunk_%03d.rds",seq_along(groups)))
chunk_receipts<-lapply(chunk_paths,function(p)list(path=normalizePath(p),sha256=file_sha(p),bytes=file.info(p)$size))
chunk_receipt_path<-file.path(output,"normalization_chunk_receipts.json")
write_json(chunk_receipts,chunk_receipt_path,auto_unbox=TRUE,pretty=TRUE)
qc<-dp_stream_qc(chunk_paths,sample_info)
sample_info$detection_failure_fraction<-qc$failure
sample_info$included<-qc$included
sample_info$inclusion_reason<-ifelse(qc$included,"Complete pair; each sample detection failure <=5%","At least one paired sample failed detection QC; both excluded")
annotation<-readRDS(file.path(output,"probe_annotation.rds"))
annotation<-annotation[match(qc$feature_ids,rownames(annotation)),,drop=FALSE]
if(anyNA(rownames(annotation))||!identical(rownames(annotation),qc$feature_ids))stop("Probe annotation identity mismatch")
snp_bad<-rep(FALSE,nrow(annotation))
for(col in intersect(c("CpG_rs","SBE_rs"),names(annotation)))snp_bad<-snp_bad|(!is.na(annotation[[col]])&nzchar(as.character(annotation[[col]])))
autosomal<-!as.character(annotation$chr)%in%c("chrX","chrY","X","Y")
is_cpg<-grepl("^cg[0-9]+$",qc$feature_ids)
selected<-qc$detection_pass&!snp_bad&autosomal&qc$finite&is_cpg
probe_audit<-data.frame(feature_id=qc$feature_ids,detection_pass=qc$detection_pass,detection_pass_count=qc$detected_count,
 included_assays=sum(sample_info$included),included=selected,
 exclusion_reason=ifelse(!qc$detection_pass,"Detection p>.01 in more than 5% of included assays",ifelse(!is_cpg,"Non-CpG identifier",ifelse(snp_bad,"Manufacturer SNP-affected CpG/SBE annotation",ifelse(!autosomal,"Sex chromosome",ifelse(!qc$finite,"Nonfinite value in included assay","Included CpG"))))),stringsAsFactors=FALSE)
write.table(probe_audit,file.path(output,"probe_inclusion.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
feature_ids<-qc$feature_ids[selected];annotation<-annotation[selected,,drop=FALSE]
included_samples<-sample_info$sample_id[sample_info$included]
people<-unique(sample_info$subject_id[sample_info$included])
baseline<-match(paste(people,0),paste(sample_info$subject_id,sample_info$time_months))
followup<-match(paste(people,18),paste(sample_info$subject_id,sample_info$time_months))
if(anyNA(c(baseline,followup)))stop("Incomplete QC-filtered individual pairs")
person_info<-sample_info[baseline,];rownames(person_info)<-people
person_info$arm<-factor(person_info$arm,levels=c("HDG","MED","greenMED"))
for(field in c("plate","position")) {
 for(visit in c("baseline","followup")) {
  value<-trimws(sample_info[[field]][if(visit=="baseline")baseline else followup])
  value[!is.na(value)&!nzchar(value)]<-NA_character_
  person_info[[paste0(visit,"_",field)]]<-factor(value)
 }
}
known_sex<-!is.na(sample_info$sex[baseline])&!is.na(sample_info$sex[followup])
if(any(sample_info$sex[baseline][known_sex]!=sample_info$sex[followup][known_sex]))stop("Sex metadata disagrees within a person")
sex<-trimws(person_info$sex);sex[!is.na(sex)&!nzchar(sex)]<-NA_character_;person_info$sex<-factor(sex)
person_info$source_age_unconverted<-suppressWarnings(as.numeric(person_info$source_age))
if(any(!is.finite(person_info$source_age_unconverted)))person_info$source_age_unconverted[!is.finite(person_info$source_age_unconverted)]<-NA_real_
# SDRF age Unit[time unit] is not resolved here; a covariate's affine scaling
# does not change the adjusted arm contrast. Do not report this field as years.
technical<-c("baseline_plate","followup_plate","baseline_position","followup_position")
demographic<-c("source_age_unconverted","sex")
models<-list(primary=dp_design(person_info,label="primary"),
 technical=dp_design(person_info,technical,"technical"),
 demographic=dp_design(person_info,demographic,"demographic"),
 technical_demographic=dp_design(person_info,c(technical,demographic),"technical_demographic"))
model_audits<-lapply(models,function(x)x[setdiff(names(x),c("design","contrasts"))])
write_json(list(models=model_audits,age_field="Unconverted SDRF baseline Characteristics[age]; contradictory time-unit annotation requires source review; no claimed years or age effect",
 technical_scope="Both baseline and followup plate and Sentrix position; no blind batch removal",
 interpretation="Primary is paired diet-pattern arm change; sensitivities separate. Estimability is not scientific approval or proof of no batch bias."),
 file.path(output,"technical_identifiability.json"),auto_unbox=TRUE,pretty=TRUE,na="null")
active<-names(models)[vapply(models,function(x)identical(x$status,"estimable_pending_scientific_review"),logical(1))]
if(!"primary"%in%active)stop("Primary arm contrasts are not estimable")
# Tiles append each sample chunk once to disk; no simultaneous 512-assay M/beta matrices.
tile_root<-file.path(output,"matrix_tiles")
tile_manifest_path<-file.path(output,"matrix_tiles.rds")
if(file.exists(tile_manifest_path)) {
 tile_record<-readRDS(tile_manifest_path)
 if(!identical(tile_record$checkpoint_key,checkpoint_key)||!identical(tile_record$feature_ids,feature_ids)||!identical(tile_record$sample_ids,included_samples))stop("Matrix tile checkpoint differs")
 tiles<-tile_record$tiles
} else {
 if(dir.exists(tile_root)&&length(list.files(tile_root)))stop("Incomplete tile staging retained; use a new output or explicitly archive failed tile stage before resuming")
 block_size<-as.integer(arg_value("--feature-block-size","20000"))
 if(!is.finite(block_size)||block_size<1L||block_size>100000L)stop("feature-block-size must be 1..100000")
 tiles<-dp_write_tiles(chunk_paths,included_samples,feature_ids,tile_root,block_size)
 dp_atomic_rds(list(checkpoint_key=checkpoint_key,feature_ids=feature_ids,sample_ids=included_samples,tiles=tiles),tile_manifest_path)
}
write_json(list(checkpoint_key=checkpoint_key,tiles=tiles),file.path(output,"matrix_tiles.json"),auto_unbox=TRUE,pretty=TRUE)
if(any(vapply(chunk_paths,file_sha,character(1))!=vapply(chunk_receipts,function(x)x$sha256,character(1))))stop("Normalized input chunk changed during QC/tile integration")
base_columns<-match(sample_info$sample_id[baseline],included_samples)
follow_columns<-match(sample_info$sample_id[followup],included_samples)
model_inputs<-list(checkpoint_key=checkpoint_key,person_info=person_info,sample_info=sample_info,
 baseline_columns=base_columns,followup_columns=follow_columns,models=models,
 matrix_manifest_path=normalizePath(tile_manifest_path),matrix_manifest_sha256=file_sha(tile_manifest_path),
 normalized_chunk_manifest_sha256=file_sha(chunk_receipt_path),
 interpretation="Actual assay tiles; subtract paired columns before model fit. No covariance or fit reconstructed from result tables.")
dp_atomic_rds(model_inputs,file.path(output,"paired_change_inputs.rds"))
block_root<-file.path(output,"unmoderated_fit_blocks");dir.create(block_root,showWarnings=FALSE)
for(k in seq_along(tiles))for(scale in c("m","beta")) {
 cat("FIT feature block",k,"of",length(tiles),"scale",scale,"\n");flush.console()
 matrix_values<-dp_read_tile(tiles[[k]],scale)
 delta<-matrix_values[,follow_columns,drop=FALSE]-matrix_values[,base_columns,drop=FALSE]
 colnames(delta)<-people;rm(matrix_values);gc(FALSE)
 for(name in active) {
  fit<-contrasts.fit(lmFit(delta,models[[name]]$design),models[[name]]$contrasts)
  fit$checkpoint_key<-checkpoint_key;fit$input_tile_sha256<-tiles[[k]]$sha256[[scale]]
  dp_atomic_rds(fit,file.path(block_root,sprintf("%s_%s_%03d.rds",name,scale,k)))
  rm(fit);gc(FALSE)
 }
 rm(delta);gc(FALSE)
}
fit_receipts<-list();result_files<-character()
for(name in active) {
 folder<-if(name=="primary")output else file.path(output,"sensitivities",name)
 dir.create(folder,recursive=TRUE,showWarnings=FALSE)
 dp_atomic_rds(list(model=models[[name]],person_info=person_info,checkpoint_key=checkpoint_key,
  paired_change_inputs_path=normalizePath(file.path(output,"paired_change_inputs.rds")),
  paired_change_inputs_sha256=file_sha(file.path(output,"paired_change_inputs.rds"))),file.path(folder,"design_and_contrasts.rds"))
 # Join all unmoderated rows BEFORE empirical Bayes; hypothesis family never splits by tile.
 fit_m<-eBayes(dp_join_fits(file.path(block_root,sprintf("%s_m_%03d.rds",name,seq_along(tiles)))),robust=TRUE)
 fit_beta<-eBayes(dp_join_fits(file.path(block_root,sprintf("%s_beta_%03d.rds",name,seq_along(tiles)))),robust=TRUE)
 fit_m$matrix_manifest_sha256<-file_sha(tile_manifest_path);fit_beta$matrix_manifest_sha256<-file_sha(tile_manifest_path)
 fit_m$model_name<-name;fit_beta$model_name<-name
 if(!identical(rownames(fit_m$coefficients),feature_ids)||!identical(rownames(fit_beta$coefficients),feature_ids))stop("Global fitted feature order changed")
 for(scale in c("m","beta"))dp_atomic_rds(if(scale=="m")fit_m else fit_beta,file.path(folder,paste0("fitted_",scale,"_global_ebayes.rds")))
 C<-models[[name]]$contrasts
 all_q<-matrix(p.adjust(as.vector(fit_m$p.value),"BH"),nrow=nrow(fit_m$p.value),dimnames=dimnames(fit_m$p.value))
 gene_labels<-if("UCSC_RefGene_Name"%in%names(annotation))as.character(annotation$UCSC_RefGene_Name) else rep(NA_character_,length(feature_ids))
 destination<-file.path(folder,"cpg_effects.tsv.gz");con<-gzfile(destination,"wt")
 for(i in seq_len(ncol(C))) {
  se<-fit_beta$stdev.unscaled[,i]*sqrt(fit_beta$s2.post);critical<-qt(.975,fit_beta$df.total)
  arms<-c("HDG","MED","greenMED")[C[c("HDG","MED","greenMED"),i]!=0];n<-sum(person_info$arm%in%arms)
  result<-data.frame(feature_type="cpg",feature_id=feature_ids,feature_label=ifelse(is.na(gene_labels)|!nzchar(gene_labels),feature_ids,gene_labels),
   contrast=colnames(C)[i],tissue="whole blood",timepoint="18-month change difference",intervention="DIRECT PLUS randomized diet-pattern comparison",
   dose=NA_character_,duration="18 months",effect_estimate=fit_beta$coefficients[,i],standard_error=se,
   confidence_low=fit_beta$coefficients[,i]-critical*se,confidence_high=fit_beta$coefficients[,i]+critical*se,
   p_value=fit_m$p.value[,i],q_value=all_q[,i],effect_unit="delta delta beta",status="measured",
   n_people=n,n_samples=sum(sample_info$included),contrast_assay_count=2*n,
   m_value_estimate=fit_m$coefficients[,i],m_value_standard_error=fit_m$stdev.unscaled[,i]*sqrt(fit_m$s2.post),
   chromosome=as.character(annotation$chr),position=as.character(annotation$pos),genome_build="hg19",annotation_gene=gene_labels,
   cell_composition_adjusted=FALSE,q_value_contrast=p.adjust(fit_m$p.value[,i],"BH"),q_value_study_family=all_q[,i],
   analysis_role=if(name=="primary")"primary" else "sensitivity",sensitivity_model=name,stringsAsFactors=FALSE)
  write.table(result,con,sep="\t",quote=FALSE,row.names=FALSE,col.names=i==1L,na="NA")
  rm(result);gc(FALSE)
 }
 close(con)
 paths<-file.path(folder,c("fitted_m_global_ebayes.rds","fitted_beta_global_ebayes.rds","design_and_contrasts.rds","cpg_effects.tsv.gz"))
 fit_receipts[[name]]<-list(model=models[[name]][setdiff(names(models[[name]]),c("design","contrasts"))],
  files=lapply(paths,function(p)list(path=normalizePath(p),sha256=file_sha(p),bytes=file.info(p)$size)))
 result_files<-c(result_files,destination);rm(fit_m,fit_beta,all_q);gc(FALSE)
}
write.table(sample_info,file.path(output,"sample_metadata.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
write.table(sample_info[,c("sample_id","subject_id","arm","time_months","detection_failure_fraction","included","inclusion_reason")],file.path(output,"sample_qc.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
if(file_sha(script)!=identity$script_sha256||file_sha(helper_path)!=identity$helper_sha256||file_sha(environment_lock)!=identity$environment_lock_sha256||file_sha(resolved_path)!=identity$resolved_source_sha256||file_sha(receipt_path)!=identity$raw_receipts_sha256||file_sha(sdrf_path)!=identity$sdrf_sha256)stop("Source/config/environment changed during analysis; no completion metadata")
metadata<-list(accession="E-MTAB-12527",analysis_version=analysis_version,run_utc=format(Sys.time(),tz="UTC",usetz=TRUE),
 script_path=normalizePath(script),script_sha256=file_sha(script),helper_path=normalizePath(helper_path),helper_sha256=file_sha(helper_path),checkpoint_key=checkpoint_key,
 environment_lock_path=normalizePath(environment_lock),environment_lock_sha256=file_sha(environment_lock),
 independent_people=length(people),sample_count=sum(sample_info$included),source_people=256,source_samples=512,
 public_paper_discrepancy="Paper reports 260 DNAm participants; public deposit has 256 complete pairs; no missing participants invented",
 result_files=list("cpg_effects.tsv.gz"),analysis_role="primary",
 result_artifacts=lapply(result_files,function(p)list(path=normalizePath(p),sha256=file_sha(p),bytes=file.info(p)$size)),
 auxiliary_files=c("sample_metadata.tsv","sample_qc.tsv","probe_inclusion.tsv","source_sample_metadata.tsv","technical_batch_balance.tsv","technical_identifiability.json","normalization_identity.json","matrix_tiles.json","paired_change_inputs.rds"),
 input_manifest=lapply(c(sdrf_path,resolved_path,receipt_path,chunk_receipt_path,tile_manifest_path,file.path(raw,"study_info.json"),file.path(raw,"study_metadata.json"),environment_lock,file.path(output,"probe_annotation.rds")),function(p)list(path=normalizePath(p),sha256=file_sha(p),bytes=file.info(p)$size)),
 normalization="minfi single-sample Noob dyeMethod=single; paired 18-month minus baseline; feature-block lmFit then global eBayes across every included CpG",
 multiple_testing="BH across all CpGs times 3 contrasts WITHIN each model; primary family separate from declared sensitivity families; per-contrast BH retained",
 beta_uncertainty="Separate beta-change global empirical Bayes fit; M-change P values, beta-scale moderated SE/95% CI",
 cell_composition_adjusted=FALSE,technical_batch_adjusted=FALSE,
 sensitivity_models=model_audits,fitted_model_artifacts=fit_receipts,
 dmr_analysis="Not performed; CpG results are not DMR discovery",pathway_analysis="Separate missMethyl analysis required; not nearest-gene enrichment",
 RNA_availability="Individual RNA/count availability not established; no paired RNA-DNAm claim",
 interpretation_limit="Whole-blood total response; no individual CpG/health prediction. Full dietary patterns cannot isolate walnut/tea/polyphenol effects. Public subset selection and SDRF age unit require source review. Technical sensitivities do not establish mediation or eliminate residual batch bias.",
 review_status="pending",scientific_approval=FALSE,package_versions=c(list(R=as.character(getRversion())),versions))
related<-list()
for(name in setdiff(active,"primary")) {
 child<-metadata;child$analysis_version<-paste0(analysis_version,"-sensitivity-",name)
 child$analysis_role<-"sensitivity";child$parent_analysis_version<-metadata$analysis_version
 child$sensitivity_model<-name;child$technical_batch_adjusted<-name%in%c("technical","technical_demographic")
 child$demographic_adjusted<-name%in%c("demographic","technical_demographic")
 child$multiple_testing<-"Sensitivity only: BH across all included CpGs times 3 contrasts within this model; separate from primary and other sensitivity families"
 child$fitted_model_artifacts<-fit_receipts[[name]]
 child$result_artifacts<-fit_receipts[[name]]$files
 child$auxiliary_files<-c("fitted_m_global_ebayes.rds","fitted_beta_global_ebayes.rds","design_and_contrasts.rds")
 child$input_manifest<-c(metadata$input_manifest,list(list(path=normalizePath(file.path(output,"paired_change_inputs.rds")),sha256=file_sha(file.path(output,"paired_change_inputs.rds")))))
 path<-file.path(output,"sensitivities",name,"analysis_metadata.json")
 write_json(child,path,auto_unbox=TRUE,pretty=TRUE,na="null")
 related[[name]]<-list(analysis_version=child$analysis_version,path=normalizePath(path),sha256=file_sha(path))
}
metadata$related_sensitivity_runs<-related
write_json(metadata,file.path(output,"analysis_metadata.json"),auto_unbox=TRUE,pretty=TRUE,na="null")
cat(toJSON(list(status="completed_analysis_scientific_review_pending",people=length(people),samples=sum(sample_info$included),cpgs=length(feature_ids),contrasts=3,sensitivity_models=setdiff(active,"primary")),auto_unbox=TRUE),"\n")
