# Probe-bias-corrected DNAm pathways. Unavailable/null enrichment is explicit.
script<-sub("^--file=","",grep("^--file=",commandArgs(),value=TRUE)[1])
source(file.path(dirname(script),"bridge_omics_helpers.R"))
suppressPackageStartupMessages({library(minfi);library(missMethyl)})
input<-normalizePath(arg_value("--input-dir",""),mustWork=TRUE)
output<-arg_value("--output-dir",file.path(input,"pathways-v1"));dir.create(output,recursive=TRUE,showWarnings=FALSE)
metadata<-fromJSON(file.path(input,"analysis_metadata.json"),simplifyVector=FALSE)
accession<-metadata$accession;array_type<-if(accession=="GSE107205")"450K" else "EPIC"
path<-file.path(input,"cpg_effects.tsv.gz");connection<-gzfile(path);header<-strsplit(readLines(connection,n=1),"\t")[[1]];close(connection)
classes<-setNames(rep("NULL",length(header)),header);classes[c("feature_id","contrast")]<-"character";classes[c("p_value","q_value")]<-"numeric"
tab<-read.delim(gzfile(path),check.names=FALSE,stringsAsFactors=FALSE,colClasses=classes,quote="",comment.char="")
annotation<-freeze_reactome(output);sets<-annotation$sets[lengths(annotation$sets)>=10L&lengths(annotation$sets)<=500L]
tables<-list();statuses<-list();gsPG_files<-character()
for(contrast in unique(tab$contrast)) {
 subset<-tab[tab$contrast==contrast,];tested<-subset$feature_id[is.finite(subset$p_value)]
 significant<-subset$feature_id[is.finite(subset$q_value)&subset$q_value<.05]
 if(!length(significant)) {
  statuses[[contrast]]<-list(status="not_tested_no_significant_cpgs",tested_cpgs=length(tested),selected_cpgs=0L,
   threshold="Joint study-family BH <0.05; not relaxed after observing null results")
  next
 }
 enriched<-gsameth(sig.cpg=significant,all.cpg=tested,collection=sets,array.type=array_type,
  prior.prob=TRUE,equiv.cpg=TRUE,fract.counts=TRUE,sig.genes=TRUE)
 tables[[contrast]]<-data.frame(feature_type="pathway",feature_id=rownames(enriched),feature_label=unname(annotation$names[rownames(enriched)]),
  contrast=contrast,tissue="whole blood",timepoint=if(accession=="GSE107205")"5-year change difference" else "18-month change difference",
  intervention="Diet-arm DNAm contrast; probe-bias-corrected gene-set over-representation",dose=NA_character_,
  duration=if(accession=="GSE107205")"5 years" else "18 months",effect_estimate=NA_real_,standard_error=NA_real_,
  p_value=enriched$P.DE,q_value=enriched$FDR,effect_unit="over-representation; no directional effect estimate",status="measured",
  n_people=metadata$independent_people,n_samples=metadata$sample_count,
  metadata_json=vapply(seq_len(nrow(enriched)),function(j)as.character(toJSON(list(method="missMethyl::gsameth",n_genes=enriched$N[j],n_altered_genes=enriched$DE[j],
   contributing_genes=if("SigGenesInSet"%in%names(enriched))enriched$SigGenesInSet[j] else NA_character_,
   gene_set_sha256=annotation$annotation$file_sha256,inference_scope="Enrichment of altered CpGs; no pathway activation or causal CpG-to-gene claim"),auto_unbox=TRUE)),character(1)))
 statuses[[contrast]]<-list(status="tested",tested_cpgs=length(tested),selected_cpgs=length(significant),tested_pathways=nrow(enriched))
}
if(length(tables)) {
 result<-do.call(rbind,tables);result$q_value_contrast<-result$q_value;result$q_value_study_family<-p.adjust(result$p_value,"BH");result$q_value<-result$q_value_study_family
 write_gzip_tsv(result,file.path(output,"pathway_effects.tsv.gz"))
} else {
 result<-data.frame();connection<-gzfile(file.path(output,"pathway_effects.tsv.gz"),"wt");writeLines("feature_type\tfeature_id\tcontrast\tp_value\tq_value",connection);close(connection)
}
if(accession=="E-MTAB-12527" && requireNamespace("dmGsea",quietly=TRUE)) {
 paired<-readRDS(file.path(input,"paired_change_inputs.rds"));fitted<-lmFit(paired$delta_m,paired$design)
 residual_m<-paired$delta_m-fitted$coefficients%*%t(paired$design)
 for(contrast in unique(tab$contrast)) {
  subset<-tab[tab$contrast==contrast,];subset<-subset[is.finite(subset$p_value),]
  outfile<-file.path(output,paste0("gsPG_",contrast))
  status<-tryCatch({
   dmGsea::gsPG(probe.p=data.frame(Name=subset$feature_id,p=subset$p_value),Data4Cor=residual_m[subset$feature_id,,drop=FALSE],
    FDRthre=.05,arrayType="EPIC",geneSet=sets,combpMethod="fisher",combpAdjust="nyholt",MonteCarlo=FALSE,ncore=4,outfile=outfile)
   "completed"
  },error=function(e)paste("not_estimable",conditionMessage(e)))
  statuses[[paste0(contrast,"_gsPG")]]<-list(status=status,FDRthre=.05,threshold_changed=FALSE,method="gsPG probe grouping; correlation from independent-person residual paired M changes")
 }
 gsPG_files<-basename(list.files(output,pattern="^gsPG_.*[.]csv$",full.names=TRUE))
}
run<-list(accession=accession,analysis_version=paste0(metadata$analysis_version,"-missMethyl-pathways-v1"),
 run_utc=format(Sys.time(),tz="UTC",usetz=TRUE),status=if(nrow(result))"completed" else "completed_no_admissible_results",
 independent_people=metadata$independent_people,sample_count=metadata$sample_count,
 script_path=normalizePath(script),script_sha256=file_sha(script),helper_path=file.path(dirname(script),"bridge_omics_helpers.R"),
 helper_sha256=file_sha(file.path(dirname(script),"bridge_omics_helpers.R")),result_files=c("pathway_effects.tsv.gz"),
 auxiliary_files=c("pathway_status.json","reactome_membership.tsv","reactome_snapshot.json",gsPG_files),
 input_manifest=lapply(c(path,file.path(input,"analysis_metadata.json")),function(p)list(path=p,sha256=file_sha(p),bytes=file.info(p)$size)),
 multiple_testing="Primary BH across pathways and all primary contrasts; within-contrast BH retained; gsPG sensitivity is separate",
 interpretation_limit="DNAm-associated gene-set over-representation is not expression activation, health benefit, or proof that nearest-gene annotation regulates that gene",
 package_versions=list(missMethyl=as.character(packageVersion("missMethyl")),dmGsea=if(requireNamespace("dmGsea",quietly=TRUE))as.character(packageVersion("dmGsea")) else NA_character_))
write_json(statuses,file.path(output,"pathway_status.json"),pretty=TRUE,auto_unbox=TRUE)
write_json(run,file.path(output,"analysis_metadata.json"),pretty=TRUE,auto_unbox=TRUE)
cat(toJSON(list(status=run$status,accession=accession,pathway_rows=nrow(result)),auto_unbox=TRUE),"\n")
