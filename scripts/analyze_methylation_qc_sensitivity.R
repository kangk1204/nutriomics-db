# Paired-person exclusion sensitivity using frozen normalized DNAm matrices.
script <- sub("^--file=", "", grep("^--file=", commandArgs(), value=TRUE)[1])
source(file.path(dirname(script), "bridge_omics_helpers.R"))
input <- normalizePath(arg_value("--input-dir", ""), mustWork=TRUE)
output <- arg_value("--output-dir", file.path(input, "qc-pair-exclusion-v1"))
dir.create(output, recursive=TRUE, showWarnings=FALSE)
original <- fromJSON(file.path(input, "analysis_metadata.json"), simplifyVector=FALSE)
paired <- readRDS(file.path(input, "paired_change_inputs.rds"))
original_probes <- rownames(paired$delta_m)
cpg_keep <- grepl("^cg[0-9]+$",original_probes)
paired$delta_m <- paired$delta_m[cpg_keep,,drop=FALSE]
paired$delta_beta <- paired$delta_beta[cpg_keep,,drop=FALSE]
qc <- read.delim(file.path(input, "sample_qc.tsv"), stringsAsFactors=FALSE)
bad <- unique(qc$subject_id[!is.finite(qc$detection_failure_fraction) | qc$detection_failure_fraction > .05])
person_key <- if ("subject_id" %in% names(paired$person_info)) as.character(paired$person_info$subject_id) else as.character(paired$person_info$person_id)
keep <- !person_key %in% bad
stopifnot(sum(keep) > ncol(paired$design), length(keep) == ncol(paired$delta_m))
design <- paired$design[keep,,drop=FALSE]
contrasts <- paired$contrasts
stopifnot(qr(design)$rank == ncol(design))
fit <- eBayes(contrasts.fit(lmFit(paired$delta_m[,keep,drop=FALSE], design), contrasts), robust=TRUE)
beta_fit <- eBayes(contrasts.fit(lmFit(paired$delta_beta[,keep,drop=FALSE], design), contrasts), robust=TRUE)
path <- file.path(input, "cpg_effects.tsv.gz")
con <- gzfile(path); header <- strsplit(readLines(con,n=1), "\t")[[1]];close(con)
classes <- setNames(rep("NULL",length(header)),header)
classes[intersect(c("feature_id","feature_label","annotation_gene","genome_build","chromosome","position"), header)] <- "character"
annot <- read.delim(gzfile(path),check.names=FALSE,stringsAsFactors=FALSE,colClasses=classes,quote="",comment.char="")
annot <- annot[!duplicated(annot$feature_id),];annot <- annot[match(rownames(fit),annot$feature_id),]
stopifnot(!anyNA(annot$feature_id))
tables <- lapply(seq_len(ncol(contrasts)),function(k) {
 involved <- rowSums(abs(design[,contrasts[,k] != 0,drop=FALSE])) > 0
 beta_se <- beta_fit$stdev.unscaled[,k]*sqrt(beta_fit$s2.post)
 critical <- qt(.975,df=beta_fit$df.total)
 data.frame(feature_type="cpg",feature_id=annot$feature_id,feature_label=annot$feature_label,
  contrast=colnames(contrasts)[k],tissue="whole blood",timepoint="5-year paired change difference",
  intervention="Diet-arm comparison after excluding both assays of people with sample detection failures >5%",
  dose=NA_character_,duration="5 years",effect_estimate=beta_fit$coefficients[,k],standard_error=beta_se,
  confidence_low=beta_fit$coefficients[,k]-critical*beta_se,confidence_high=beta_fit$coefficients[,k]+critical*beta_se,
  p_value=fit$p.value[,k],q_value=p.adjust(fit$p.value[,k],"BH"),effect_unit="delta beta",status="measured",
  n_people=sum(involved),n_samples=2*sum(keep),contrast_assay_count=2*sum(involved),
  m_value_estimate=fit$coefficients[,k],m_value_standard_error=fit$stdev.unscaled[,k]*sqrt(fit$s2.post),
  genome_build=annot$genome_build,annotation_gene=annot$annotation_gene,chromosome=annot$chromosome,position=annot$position,
  cell_composition_adjusted=FALSE,stringsAsFactors=FALSE)
})
result <- do.call(rbind,tables);result$q_value_contrast <- result$q_value
result$q_value_study_family <- p.adjust(result$p_value,"BH");result$q_value <- result$q_value_study_family
write_gzip_tsv(result,file.path(output,"cpg_effects.tsv.gz"))
qc$included <- !qc$subject_id %in% bad
qc$inclusion_reason <- ifelse(qc$included,"Both paired samples have detection failure <=5%", "Person excluded because one or more paired samples failed detection threshold")
write.table(qc,file.path(output,"sample_qc.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
write.table(paired$person_info[keep,],file.path(output,"person_metadata.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
write.table(data.frame(feature_id=original_probes,included=cpg_keep,reason=ifelse(cpg_keep,"CpG identifier cg", "Non-CpG ch/rs identifier excluded")),file.path(output,"probe_inclusion.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
saveRDS(list(delta_m=paired$delta_m[,keep,drop=FALSE],delta_beta=paired$delta_beta[,keep,drop=FALSE],person_info=paired$person_info[keep,],design=design,contrasts=contrasts),file.path(output,"paired_change_inputs.rds"),compress=FALSE)
original$analysis_version <- arg_value("--analysis-version",paste0(original$analysis_version,"-qc-pair-exclusion-v1"))
original$run_utc <- format(Sys.time(),tz="UTC",usetz=TRUE)
original$script_path <- normalizePath(script);original$script_sha256 <- file_sha(script)
original$helper_path <- file.path(dirname(script),"bridge_omics_helpers.R");original$helper_sha256 <- file_sha(original$helper_path)
original$sample_count <- 2*sum(keep);original$independent_people <- sum(keep)
original$excluded_people <- as.list(bad)
original$design_formula <- "One paired change per QC-eligible person; original arm contrast; both assays excluded for a failed member of the pair"
original$analysis_role <- "QC-clean paired analysis; all-public-person result preserved as a separate earlier version"
original$probe_filter <- "Conservative fixed tested-probe set from the original 72-array Noob analysis; no re-selection after observing this sensitivity"
original$input_manifest <- lapply(c(path,file.path(input,"analysis_metadata.json"),file.path(input,"paired_change_inputs.rds"),file.path(input,"sample_qc.tsv")),function(p)list(path=p,sha256=file_sha(p),bytes=file.info(p)$size))
original$result_files <- list("cpg_effects.tsv.gz");original$auxiliary_files <- list("sample_qc.tsv","person_metadata.tsv","probe_inclusion.tsv")
write_json(original,file.path(output,"analysis_metadata.json"),auto_unbox=TRUE,pretty=TRUE)
cat(toJSON(list(status="completed",independent_people=sum(keep),excluded_people=length(bad),effect_rows=nrow(result),significant_joint_fdr=sum(result$q_value<.05,na.rm=TRUE)),auto_unbox=TRUE),"\n")
