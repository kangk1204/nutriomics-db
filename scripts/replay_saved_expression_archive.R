# Replay only the already-normalized microarray input; never substitute GEO series.
script<-sub("^--file=","",grep("^--file=",commandArgs(),value=TRUE)[1L])
source(file.path(dirname(script),"expression_archive_helpers.R"))
suppressPackageStartupMessages({library(limma);library(jsonlite)})
args<-commandArgs(trailingOnly=TRUE)
arg<-function(name){at<-match(name,args);if(is.na(at)||at==length(args))stop("Missing ",name);args[[at+1L]]}
source_rds<-normalizePath(arg("--source-rds"),mustWork=TRUE)
parent_path<-normalizePath(arg("--parent-metadata"),mustWork=TRUE)
reference_path<-normalizePath(arg("--reference-gene-effects"),mustWork=TRUE)
lock_path<-normalizePath(arg("--environment-lock"),mustWork=TRUE)
output<-arg("--output-dir");version<-arg("--archive-version")
if(file.exists(output))stop("Use a NEW archive output directory")
if(file.info(source_rds)$size>32*1024^2)stop("Only selected small saved-normalized input is allowed")
for(entry in list(c(source_rds,arg("--expected-sha256")),c(parent_path,arg("--expected-parent-sha256")),
    c(reference_path,arg("--expected-reference-sha256")),c(lock_path,arg("--expected-environment-sha256"))))
  if(expression_archive_sha256(entry[1L])!=entry[2L])stop("Source hash mismatch: ",entry[1L])
parent<-fromJSON(parent_path,simplifyVector=FALSE)
if(!parent$accession%in%c("GSE13466","GSE27385"))stop("No unweighted RNA-seq/unknown-study fallback is permitted")
if(!is.null(parent$package_versions$limma)&&parent$package_versions$limma!=as.character(packageVersion("limma")))stop("Locked limma differs from actual parent")
if(arg("--q-scope")!="study_joint")stop("This selected replay requires the original documented joint gene/contrast BH family")
state<-readRDS(source_rds)
if(any(!c("expression","sample_info","design","contrasts")%in%names(state)))stop("Actual saved expression/model inputs are incomplete")
if(qr(state$design)$rank!=ncol(state$design))stop("Saved model is not full rank")
fit0<-lmFit(state$expression,state$design)
fit<-eBayes(contrasts.fit(fit0,state$contrasts),robust=TRUE)
reference<-read.delim(gzfile(reference_path),check.names=FALSE,stringsAsFactors=FALSE,quote="",comment.char="")
required<-c("feature_id","contrast","effect_estimate","standard_error","confidence_low","confidence_high","p_value","q_value")
if(!all(required%in%names(reference))||nrow(reference)!=nrow(state$expression)*ncol(state$contrasts)||
    anyDuplicated(paste(reference$feature_id,reference$contrast)))stop("Reference effect table keys/fields incomplete")
joint<-matrix(p.adjust(as.vector(fit$p.value),"BH"),nrow=nrow(fit$p.value),dimnames=dimnames(fit$p.value))
comparisons<-list()
for(k in seq_len(ncol(state$contrasts))) {
  keys<-paste(rownames(state$expression),colnames(state$contrasts)[k])
  index<-match(keys,paste(reference$feature_id,reference$contrast))
  if(anyNA(index))stop("Saved features/contrasts do not match reference keys")
  se<-fit$stdev.unscaled[,k]*sqrt(fit$s2.post);critical<-qt(.975,fit$df.total)
  expected<-list(effect_estimate=fit$coefficients[,k],standard_error=se,
    confidence_low=fit$coefficients[,k]-critical*se,confidence_high=fit$coefficients[,k]+critical*se,
    p_value=fit$p.value[,k],q_value=joint[,k])
  if("q_value_contrast"%in%names(reference))expected$q_value_contrast<-p.adjust(fit$p.value[,k],"BH")
  if("q_value_study_family"%in%names(reference))expected$q_value_study_family<-joint[,k]
  fields<-list()
  for(name in names(expected)) {
    observed<-as.numeric(reference[[name]][index]);values<-unname(expected[[name]])
    if(!identical(is.na(observed),is.na(values))||any(!is.finite(values))||
       !isTRUE(all.equal(observed,values,tolerance=1e-12,check.attributes=FALSE)))
      stop("Numeric archive replay differs from original ",colnames(state$contrasts)[k],"/",name)
    fields[[name]]<-list(values=length(values),max_absolute_difference=max(abs(observed-values)),
      exactly_equal_binary_after_TSV_parse=identical(observed,values))
  }
  comparisons[[colnames(state$contrasts)[k]]]<-fields
}
for(entry in list(c(source_rds,arg("--expected-sha256")),c(parent_path,arg("--expected-parent-sha256")),
    c(reference_path,arg("--expected-reference-sha256")),c(lock_path,arg("--expected-environment-sha256"))))
  if(expression_archive_sha256(entry[1L])!=entry[2L])stop("Source changed during archive replay")
dir.create(output,recursive=TRUE)
record<-save_expression_model_archive(state$expression,state$sample_info,state$design,state$contrasts,fit0,fit,
 file.path(output,"reanalysis_inputs.rds"),parent_provenance=list(
  source_rds=source_rds,source_rds_sha256=arg("--expected-sha256"),parent_metadata=parent_path,
  parent_metadata_sha256=arg("--expected-parent-sha256"),parent_analysis_version=parent$analysis_version,
  reference_gene_effects=reference_path,reference_sha256=arg("--expected-reference-sha256")))
metadata<-list(status="archive_replay_gene_numeric_parity_checked_expert_pending",archive_version=version,
  role="Model archive supplement; not a new cohort, normalization run or duplicated effect analysis",
  accession=parent$accession,parent_analysis_version=parent$analysis_version,
  script_path=normalizePath(script),script_sha256=expression_archive_sha256(script),
  helper_path=normalizePath(file.path(dirname(script),"expression_archive_helpers.R")),
  helper_sha256=expression_archive_sha256(file.path(dirname(script),"expression_archive_helpers.R")),
  environment_lock_path=lock_path,environment_lock_sha256=expression_archive_sha256(lock_path),
  source_rds=source_rds,source_rds_sha256=arg("--expected-sha256"),
  parent_metadata_sha256=arg("--expected-parent-sha256"),model_archive=record,
  original_result_tables_regenerated=FALSE,gene_statistics_recomputed=TRUE,
  pathway_statistics_recomputed=FALSE,comparison_tolerance=1e-12,comparison= comparisons,
  limitation="Actual saved raw-RMA expression was reused. This verifies fitting conditional on those inputs, not independent raw normalization or biological validity. No depositor matrix substitution.",
  expert_review_status="pending",scientific_approval=FALSE,run_utc=format(Sys.time(),tz="UTC",usetz=TRUE),
  package_versions=list(R=as.character(getRversion()),limma=as.character(packageVersion("limma"))))
write_json(metadata,file.path(output,"archive_metadata.json"),auto_unbox=TRUE,pretty=TRUE)
cat(toJSON(list(status=metadata$status,source_samples=ncol(state$expression),features=nrow(state$expression),contrasts=ncol(state$contrasts)),auto_unbox=TRUE),"\n")
