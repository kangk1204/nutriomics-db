# Preserve original fits and apply BH across all four pre-specified study contrasts.
script<-sub("^--file=","",grep("^--file=",commandArgs(),value=TRUE)[1])
source(file.path(dirname(script),"bridge_omics_helpers.R"))
root<-arg_value("--root",Sys.getenv("NUTRIOMICS_ANALYSIS_ROOT", unset="expansion"))
folders<-file.path(root,"outputs",c("gse98645-acute-v1","gse98645-chronic-v1"))
outputs<-file.path(root,"outputs",c("gse98645-acute-studyfamily-v2","gse98645-chronic-studyfamily-v2"))
for(p in outputs)dir.create(p,recursive=TRUE,showWarnings=FALSE)
inputs<-file.path(rep(folders,each=3),rep(c("gene_effects.tsv.gz","pathway_effects.tsv.gz","camera_sensitivity.tsv.gz"),2))
for(filename in c("gene_effects.tsv.gz","pathway_effects.tsv.gz","camera_sensitivity.tsv.gz")) {
 tables<-lapply(folders,function(p)read.delim(gzfile(file.path(p,filename)),check.names=FALSE,stringsAsFactors=FALSE,quote="",comment.char=""))
 column<-if(filename=="camera_sensitivity.tsv.gz")"PValue" else "p_value"
 q<-p.adjust(unlist(lapply(tables,function(x)x[[column]]),use.names=FALSE),"BH")
 offset<-0L
 for(k in seq_along(tables)) {
  rows<-seq_len(nrow(tables[[k]]));adjusted<-q[offset+rows];offset<-offset+length(rows)
  if(filename=="camera_sensitivity.tsv.gz") {
   tables[[k]]$FDR_analysis_family<-tables[[k]]$FDR_study_family;tables[[k]]$FDR_study_family<-adjusted
  } else {
   tables[[k]]$q_value_analysis_family<-tables[[k]]$q_value_study_family
   tables[[k]]$q_value_study_family<-adjusted;tables[[k]]$q_value<-adjusted
  }
  write_gzip_tsv(tables[[k]],file.path(outputs[[k]],filename))
 }
}
for(k in seq_along(folders)) {
 original<-fromJSON(file.path(folders[[k]],"analysis_metadata.json"),simplifyVector=FALSE)
 for(filename in unlist(original$auxiliary_files)) {
  if(filename!="camera_sensitivity.tsv.gz")file.copy(file.path(folders[[k]],filename),file.path(outputs[[k]],filename),overwrite=TRUE)
 }
 file.copy(file.path(folders[[k]],"reanalysis_inputs.rds"),file.path(outputs[[k]],"reanalysis_inputs.rds"),overwrite=TRUE)
 original$analysis_version<-paste0(sub("-v1$","-studyfamily-v2",original$analysis_version),arg_value("--analysis-version-suffix",""))
 original$run_utc<-format(Sys.time(),tz="UTC",usetz=TRUE)
 original$script_path<-normalizePath(script);original$script_sha256<-file_sha(script)
 original$helper_path<-file.path(dirname(script),"bridge_omics_helpers.R");original$helper_sha256<-file_sha(original$helper_path)
 original$multiple_testing<-"Primary BH across acute 2/4/6h and chronic 2-week gene tests jointly; pathway and CAMERA families separately span the same four contrasts. Within-contrast and original analysis-family q-values preserved."
 original$derived_analysis<-"Expression fits, effects, uncertainties and seeded FGSEA statistics unchanged; study-wide multiplicity corrected"
 original$input_manifest<-lapply(c(inputs,file.path(folders,"analysis_metadata.json")),function(p)list(path=p,bytes=file.info(p)$size,sha256=file_sha(p)))
 write_json(original,file.path(outputs[[k]],"analysis_metadata.json"),auto_unbox=TRUE,pretty=TRUE)
}
cat(toJSON(list(status="completed",outputs=outputs,primary_contrasts=4),auto_unbox=TRUE),"\n")
