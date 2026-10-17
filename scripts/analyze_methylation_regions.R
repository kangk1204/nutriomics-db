# Prespecified fixed-bin region screen; every eligible region is tested.
# Averages are per person, so correlated CpGs do not become independent samples.
script<-sub("^--file=","",grep("^--file=",commandArgs(),value=TRUE)[1])
source(file.path(dirname(script),"bridge_omics_helpers.R"))
input<-normalizePath(arg_value("--input-dir",""),mustWork=TRUE)
output<-arg_value("--output-dir",file.path(input,"regions-fixed1000bp-v1"));dir.create(output,recursive=TRUE,showWarnings=FALSE)
metadata<-fromJSON(file.path(input,"analysis_metadata.json"),simplifyVector=FALSE)
paired<-readRDS(file.path(input,"paired_change_inputs.rds"))
path<-file.path(input,"cpg_effects.tsv.gz");con<-gzfile(path);header<-strsplit(readLines(con,n=1),"\t")[[1]];close(con)
classes<-setNames(rep("NULL",length(header)),header);classes[c("feature_id","chromosome","position")]<-"character"
annot<-read.delim(gzfile(path),stringsAsFactors=FALSE,check.names=FALSE,colClasses=classes,quote="",comment.char="")
annot<-annot[!duplicated(annot$feature_id),];annot<-annot[match(rownames(paired$delta_m),annot$feature_id),]
position<-suppressWarnings(as.integer(annot$position));chromosome<-as.character(annot$chromosome)
valid<-grepl("^cg[0-9]+$",annot$feature_id)&is.finite(position)&position>0&grepl("^chr([1-9]|1[0-9]|2[0-2])$",chromosome)
stopifnot(sum(valid)>100000)
start<-floor((position[valid]-1)/1000)*1000+1
region<-paste0(chromosome[valid],":",start,"-",start+999)
counts<-table(region);eligible<-names(counts[counts>=3])
average_regions<-function(matrix) {
 summed<-rowsum(matrix[valid,,drop=FALSE],region,reorder=TRUE)
 result<-sweep(summed,1,as.numeric(counts[rownames(summed)]),"/")
 result[eligible,,drop=FALSE]
}
region_m<-average_regions(paired$delta_m);region_beta<-average_regions(paired$delta_beta)
fit<-eBayes(contrasts.fit(lmFit(region_m,paired$design),paired$contrasts),robust=TRUE)
beta_fit<-eBayes(contrasts.fit(lmFit(region_beta,paired$design),paired$contrasts),robust=TRUE)
membership<-data.frame(feature_id=annot$feature_id[valid],region_id=region,eligible=region%in%eligible,stringsAsFactors=FALSE)
write_gzip_tsv(membership,file.path(output,"region_membership.tsv.gz"))
tables<-lapply(seq_len(ncol(paired$contrasts)),function(k) {
 se<-beta_fit$stdev.unscaled[,k]*sqrt(beta_fit$s2.post);critical<-qt(.975,beta_fit$df.total)
 involved<-rowSums(abs(paired$design[,paired$contrasts[,k]!=0,drop=FALSE]))>0
 data.frame(feature_type="dmr",feature_id=eligible,feature_label=paste0("hg19 fixed 1000-bp region ",eligible),
  contrast=colnames(paired$contrasts)[k],tissue="whole blood",timepoint=if(metadata$accession=="GSE107205")"5-year paired change difference" else "18-month paired change difference",
  intervention="Diet-arm comparison of person-level mean methylation changes in prespecified regions",dose=NA_character_,duration=if(metadata$accession=="GSE107205")"5 years" else "18 months",
  effect_estimate=beta_fit$coefficients[,k],standard_error=se,confidence_low=beta_fit$coefficients[,k]-critical*se,confidence_high=beta_fit$coefficients[,k]+critical*se,
  p_value=fit$p.value[,k],q_value=p.adjust(fit$p.value[,k],"BH"),effect_unit="mean region delta delta beta",status="measured",
  n_people=sum(involved),n_samples=metadata$sample_count,m_value_estimate=fit$coefficients[,k],genome_build="hg19",n_cpg=as.integer(counts[eligible]),
  metadata_json=vapply(eligible,function(id)as.character(toJSON(list(method="Prespecified fixed 1000-bp bins; at least 3 tested CpGs; equal CpG mean per person; limma paired-change arm contrast",
   region_selection="Coordinates and measured probe membership only, before inspecting effect or p-values",interpretation="Regional screening endpoint; boundaries are fixed tiles, not data-driven DMRcate segmentation"),auto_unbox=TRUE)),character(1)),stringsAsFactors=FALSE)
})
result<-do.call(rbind,tables);result$q_value_contrast<-result$q_value;result$q_value_study_family<-p.adjust(result$p_value,"BH");result$q_value<-result$q_value_study_family
write_gzip_tsv(result,file.path(output,"dmr_effects.tsv.gz"))
metadata$analysis_version<-arg_value("--analysis-version",paste0(metadata$analysis_version,"-regions-fixed1000bp-v1"));metadata$run_utc<-format(Sys.time(),tz="UTC",usetz=TRUE)
metadata$script_path<-normalizePath(script);metadata$script_sha256<-file_sha(script);metadata$helper_path<-file.path(dirname(script),"bridge_omics_helpers.R");metadata$helper_sha256<-file_sha(metadata$helper_path)
metadata$analysis_role<-"Secondary fixed-region screening endpoint; no claim that fixed tile boundaries reproduce segmented DMRs"
metadata$dmr_analysis<-"All hg19 autosomal fixed 1000-bp regions containing at least 3 tested CpGs; person-level M/beta means preserve within-region correlation; regional M tests and beta-scale SE/CI"
metadata$multiple_testing<-"Primary BH across every eligible fixed region by all study primary contrasts; within-contrast BH retained"
metadata$input_manifest<-lapply(c(path,file.path(input,"paired_change_inputs.rds"),file.path(input,"analysis_metadata.json")),function(p)list(path=p,sha256=file_sha(p),bytes=file.info(p)$size))
metadata$result_files<-list("dmr_effects.tsv.gz");metadata$auxiliary_files<-list("region_membership.tsv.gz")
write_json(metadata,file.path(output,"analysis_metadata.json"),auto_unbox=TRUE,pretty=TRUE)
cat(toJSON(list(status="completed",eligible_regions=length(eligible),effect_rows=nrow(result),joint_fdr_lt_005=sum(result$q_value<.05,na.rm=TRUE)),auto_unbox=TRUE),"\n")
