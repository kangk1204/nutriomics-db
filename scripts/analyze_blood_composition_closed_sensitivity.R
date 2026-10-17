# Reuse frozen reference estimates and explicitly model relative composition.
# Raw Houseman estimates need not sum to one; close them before omitting Gran.
script<-sub("^--file=","",grep("^--file=",commandArgs(),value=TRUE)[1])
source(file.path(dirname(script),"bridge_omics_helpers.R"))
input<-normalizePath(arg_value("--input-dir",""),mustWork=TRUE)
output<-arg_value("--output-dir",file.path(input,"blood-composition-closed-v2"));dir.create(output,recursive=TRUE,showWarnings=FALSE)
estimates<-normalizePath(file.path(input,"blood-composition-v1"),mustWork=TRUE)
source_done<-fromJSON(file.path(estimates,"DONE.json"),simplifyVector=FALSE)
for(name in c("estimated_cell_proportions.tsv","analysis_metadata.json")) {
 record<-Filter(function(x)x$path==name,source_done$files)
 stopifnot(length(record)==1L,file_sha(file.path(estimates,name))==record[[1]]$sha256)
}
metadata<-fromJSON(file.path(input,"analysis_metadata.json"),simplifyVector=FALSE)
reference_metadata<-fromJSON(file.path(estimates,"analysis_metadata.json"),simplifyVector=FALSE)
paired<-readRDS(file.path(input,"paired_change_inputs.rds"))
raw<-read.delim(file.path(estimates,"estimated_cell_proportions.tsv"),stringsAsFactors=FALSE)
types<-c("CD8T","CD4T","NK","Bcell","Mono","Gran")
raw_counts<-as.matrix(raw[,types]);stopifnot(all(is.finite(raw_counts)),all(raw_counts>=-1e-12),all(rowSums(raw_counts)>0))
roundoff_negative_count<-sum(raw_counts<0);raw_minimum<-min(raw_counts)
raw_counts[raw_counts<0]<-0 # Constrained projection's double-precision roundoff only.
counts<-raw_counts/rowSums(raw_counts)
stopifnot(max(abs(rowSums(counts)-1))<1e-12,all(counts>=0),all(counts<=1))
write.table(data.frame(sample_id=raw$sample_id,counts),file.path(output,"closed_cell_proportions.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
direct<-metadata$accession=="E-MTAB-12527"
qc<-read.delim(file.path(input,"sample_qc.tsv"),stringsAsFactors=FALSE)
samples<-if(direct)read.delim(file.path(input,"sample_metadata.tsv"),stringsAsFactors=FALSE) else read.delim(file.path(dirname(input),"gse107205-between-arm-v3","sample_metadata.tsv"),stringsAsFactors=FALSE)
id_field<-if(direct)"sample_id" else "sample_accession"
samples<-samples[samples[[id_field]]%in%qc[[id_field]][qc$included],]
stopifnot(setequal(raw$sample_id,samples[[id_field]]))
counts<-counts[match(samples[[id_field]],raw$sample_id),,drop=FALSE]
person<-as.character(paired$person_info$subject_id)
key<-paste(if(direct)as.character(samples$subject_id) else as.character(samples$person_id),if(direct)as.character(samples$time_months) else as.character(samples$timepoint))
base<-match(paste(person,if(direct)"0" else "baseline"),key)
post<-match(paste(person,if(direct)"18" else "5y"),key)
stopifnot(!anyNA(base),!anyNA(post))
cell_types<-types[types!="Gran"]
delta_counts<-counts[post,cell_types,drop=FALSE]-counts[base,cell_types,drop=FALSE]
design<-cbind(paired$design,delta_counts);colnames(design)<-make.names(colnames(design))
stopifnot(qr(design)$rank==ncol(design),nrow(design)>ncol(design)+2)
contrasts<-rbind(paired$contrasts,matrix(0,nrow=length(cell_types),ncol=ncol(paired$contrasts)))
rownames(contrasts)<-colnames(design)
fit<-eBayes(contrasts.fit(lmFit(paired$delta_m,design),contrasts),robust=TRUE)
beta_fit<-eBayes(contrasts.fit(lmFit(paired$delta_beta,design),contrasts),robust=TRUE)
path<-file.path(input,"cpg_effects.tsv.gz");con<-gzfile(path);header<-strsplit(readLines(con,n=1),"\t")[[1]];close(con)
classes<-setNames(rep("NULL",length(header)),header);classes[c("feature_id","feature_label","genome_build","annotation_gene","chromosome","position")]<-"character"
annot<-read.delim(gzfile(path),check.names=FALSE,stringsAsFactors=FALSE,colClasses=classes,quote="",comment.char="")
annot<-annot[!duplicated(annot$feature_id),];annot<-annot[match(rownames(paired$delta_m),annot$feature_id),]
stopifnot(!anyNA(annot$feature_id),all(grepl("^cg[0-9]+$",annot$feature_id)))
tables<-lapply(seq_len(ncol(contrasts)),function(k) {
 se<-beta_fit$stdev.unscaled[,k]*sqrt(beta_fit$s2.post);critical<-qt(.975,beta_fit$df.total)
 involved<-rowSums(abs(paired$design[,paired$contrasts[,k]!=0,drop=FALSE]))>0
 data.frame(feature_type="cpg",feature_id=annot$feature_id,feature_label=annot$feature_label,contrast=colnames(contrasts)[k],
  tissue="whole blood",timepoint=if(direct)"18-month paired change difference" else "5-year paired change difference",
  intervention="Diet-arm comparison adjusted for five changes in closed estimated blood cell fractions",dose=NA_character_,duration=if(direct)"18 months" else "5 years",
  effect_estimate=beta_fit$coefficients[,k],standard_error=se,confidence_low=beta_fit$coefficients[,k]-critical*se,confidence_high=beta_fit$coefficients[,k]+critical*se,
  p_value=fit$p.value[,k],q_value=p.adjust(fit$p.value[,k],"BH"),effect_unit="delta delta beta",status="measured",
  n_people=sum(involved),n_samples=nrow(samples),m_value_estimate=fit$coefficients[,k],m_value_standard_error=fit$stdev.unscaled[,k]*sqrt(fit$s2.post),
  chromosome=annot$chromosome,position=annot$position,genome_build=annot$genome_build,annotation_gene=annot$annotation_gene,cell_composition_adjusted=TRUE,stringsAsFactors=FALSE)
})
result<-do.call(rbind,tables);result$q_value_contrast<-result$q_value;result$q_value_study_family<-p.adjust(result$p_value,"BH");result$q_value<-result$q_value_study_family
write_gzip_tsv(result,file.path(output,"cpg_effects.tsv.gz"))
metadata$analysis_version<-arg_value("--analysis-version",paste0(metadata$analysis_version,"-blood-composition-closed-v2"));metadata$run_utc<-format(Sys.time(),tz="UTC",usetz=TRUE)
metadata$script_path<-normalizePath(script);metadata$script_sha256<-file_sha(script);metadata$helper_path<-file.path(dirname(script),"bridge_omics_helpers.R");metadata$helper_sha256<-file_sha(metadata$helper_path)
metadata$cell_composition_adjusted<-TRUE;metadata$analysis_role<-"Sensitivity; estimated relative cell composition may mediate dietary response; no direct-effect causal claim"
metadata$design_formula<-"Paired M/beta changes; original arm contrasts plus changes in five closed relative cell fractions; Gran reference omitted"
metadata$cell_estimation<-reference_metadata$cell_estimation
metadata$cell_estimation$closure<-"Each of six original nonnegative Houseman estimates divided by the six-estimate sample sum before deriving changes"
metadata$cell_estimation$raw_sum_range<-range(rowSums(raw_counts))
metadata$cell_estimation$numerical_roundoff<-list(tolerance=1e-12,negative_count=roundoff_negative_count,minimum_raw_estimate=raw_minimum,policy="Only estimates within 1e-12 below zero are set to zero; larger negative estimates fail the composition gate")
metadata$cell_estimation$source_counts_sha256<-file_sha(file.path(estimates,"estimated_cell_proportions.tsv"))
metadata$cell_estimation$estimation_uncertainty<-"Reference projection uncertainty is not propagated into regression SE; this is a sensitivity analysis"
metadata$interpretation_limit<-"Conditional association adjusted for estimated relative blood composition; cell composition may mediate response. No cell-intrinsic or direct-effect causal claim, individual prediction, or automatic health benefit. Primary total-response analysis remains separate."
metadata$design_audit<-list(rank=qr(design)$rank,columns=ncol(design),people=nrow(design),residual_df=nrow(design)-ncol(design))
metadata$input_manifest<-lapply(c(path,file.path(input,"paired_change_inputs.rds"),file.path(input,"analysis_metadata.json"),file.path(estimates,"estimated_cell_proportions.tsv"),file.path(estimates,"analysis_metadata.json"),file.path(estimates,"DONE.json")),function(p)list(path=normalizePath(p),sha256=file_sha(p),bytes=file.info(p)$size))
metadata$result_files<-list("cpg_effects.tsv.gz");metadata$auxiliary_files<-list("closed_cell_proportions.tsv")
write_json(metadata,file.path(output,"analysis_metadata.json"),auto_unbox=TRUE,pretty=TRUE)
cat(toJSON(list(status="completed",people=nrow(design),samples=nrow(samples),joint_fdr_lt_005=sum(result$q_value<.05,na.rm=TRUE)),auto_unbox=TRUE),"\n")
