# Reference-based blood composition sensitivity; total-response primary unchanged.
script<-sub("^--file=","",grep("^--file=",commandArgs(),value=TRUE)[1])
source(file.path(dirname(script),"bridge_omics_helpers.R"))
suppressPackageStartupMessages({library(minfi);library(FlowSorted.Blood.450k)})
input<-normalizePath(arg_value("--input-dir",""),mustWork=TRUE)
root<-normalizePath(arg_value("--root",Sys.getenv("NUTRIOMICS_ANALYSIS_ROOT", unset="expansion")))
output<-arg_value("--output-dir",file.path(input,"blood-composition-v1"));dir.create(output,recursive=TRUE,showWarnings=FALSE)
metadata<-fromJSON(file.path(input,"analysis_metadata.json"),simplifyVector=FALSE)
paired<-readRDS(file.path(input,"paired_change_inputs.rds"))
direct<-metadata$accession=="E-MTAB-12527"
qc<-read.delim(file.path(input,"sample_qc.tsv"),stringsAsFactors=FALSE)
samples<-if(direct)read.delim(file.path(input,"sample_metadata.tsv"),stringsAsFactors=FALSE) else read.delim(file.path(dirname(input),"gse107205-between-arm-v3","sample_metadata.tsv"),stringsAsFactors=FALSE)
id_field<-if(direct)"sample_id" else "sample_accession"
samples<-samples[samples[[id_field]]%in%qc[[id_field]][qc$included],]
targets<-data.frame(Sample_Name=samples[[id_field]],Basename=NA_character_,stringsAsFactors=FALSE)
scratch<-tempfile("cell-reference-idat-",tmpdir=root);dir.create(scratch)
on.exit(unlink(scratch,recursive=TRUE),add=TRUE)
raw_files<-character()
if(direct) {
 targets$Basename<-file.path(root,"raw/E-MTAB-12527/idat",samples$base)
 raw_files<-as.vector(outer(targets$Basename,c("_Grn.idat","_Red.idat"),paste0))
} else {
 raw_dir<-file.path(arg_value("--data-dir",file.path(dirname(dirname(root)),"data")),"processed/geo/GSE107205/raw_files")
 for(i in seq_len(nrow(targets))) {
  files<-list.files(raw_dir,pattern=paste0("^",targets$Sample_Name[i],"_.*_(Gn|Grn|Red)[.]idat[.]gz$"),full.names=TRUE)
  stopifnot(length(files)==2L)
  targets$Basename[i]<-file.path(scratch,targets$Sample_Name[i]);raw_files<-c(raw_files,files)
  for(p in files) {
   channel<-if(grepl("_Red[.]idat",p))"Red" else "Grn"
   source<-gzfile(p,"rb");dest<-file(paste0(targets$Basename[i],"_",channel,".idat"),"wb")
   repeat {block<-readBin(source,"raw",1024L*1024L);if(!length(block))break;writeBin(block,dest)}
   close(source);close(dest)
  }
 }
}
stopifnot(all(file.exists(raw_files)))
rg<-read.metharray.exp(targets=targets,extended=FALSE,force=FALSE,verbose=TRUE)
stopifnot(identical(sampleNames(rg),basename(targets$Basename)))
sampleNames(rg)<-targets$Sample_Name
# Published Houseman/minfi reference algorithm: adult 450K sorted blood;
# EPIC arrays are converted to shared 450K probes by the documented function.
counts<-estimateCellCounts(rg,compositeCellType="Blood",referencePlatform="IlluminaHumanMethylation450k",
 processMethod="auto",probeSelect="auto",returnAll=FALSE,meanPlot=FALSE,verbose=TRUE)
rm(rg);gc()
stopifnot(all(is.finite(counts)),setequal(rownames(counts),samples[[id_field]]))
counts<-counts[match(samples[[id_field]],rownames(counts)),,drop=FALSE]
write.table(data.frame(sample_id=samples[[id_field]],counts),file.path(output,"estimated_cell_proportions.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
person_id<-as.character(paired$person_info$subject_id)
sample_key<-if(direct)as.character(samples$subject_id) else as.character(samples$person_id)
time<-if(direct)as.character(samples$time_months) else as.character(samples$timepoint)
base<-match(paste(person_id,if(direct)"0" else "baseline"),paste(sample_key,time))
post<-match(paste(person_id,if(direct)"18" else "5y"),paste(sample_key,time))
stopifnot(!anyNA(base),!anyNA(post))
cell_types<-c("CD8T","CD4T","NK","Bcell","Mono") # Gran omitted as reference
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
tables<-lapply(seq_len(ncol(contrasts)),function(k) {
 se<-beta_fit$stdev.unscaled[,k]*sqrt(beta_fit$s2.post);critical<-qt(.975,beta_fit$df.total)
 involved<-rowSums(abs(paired$design[,paired$contrasts[,k]!=0,drop=FALSE]))>0
 data.frame(feature_type="cpg",feature_id=annot$feature_id,feature_label=annot$feature_label,contrast=colnames(contrasts)[k],
  tissue="whole blood",timepoint=if(direct)"18-month paired change difference" else "5-year paired change difference",
  intervention="Diet-arm comparison adjusted for five estimated changes in blood cell fractions",dose=NA_character_,duration=if(direct)"18 months" else "5 years",
  effect_estimate=beta_fit$coefficients[,k],standard_error=se,confidence_low=beta_fit$coefficients[,k]-critical*se,confidence_high=beta_fit$coefficients[,k]+critical*se,
  p_value=fit$p.value[,k],q_value=p.adjust(fit$p.value[,k],"BH"),effect_unit="delta delta beta",status="measured",
  n_people=sum(involved),n_samples=nrow(samples),m_value_estimate=fit$coefficients[,k],m_value_standard_error=fit$stdev.unscaled[,k]*sqrt(fit$s2.post),
  chromosome=annot$chromosome,position=annot$position,genome_build=annot$genome_build,annotation_gene=annot$annotation_gene,cell_composition_adjusted=TRUE,stringsAsFactors=FALSE)
})
result<-do.call(rbind,tables);result$q_value_contrast<-result$q_value;result$q_value_study_family<-p.adjust(result$p_value,"BH");result$q_value<-result$q_value_study_family
write_gzip_tsv(result,file.path(output,"cpg_effects.tsv.gz"))
reference_dir<-system.file(package="FlowSorted.Blood.450k")
reference_files<-c(file.path(reference_dir,"DESCRIPTION"),list.files(file.path(reference_dir,"data"),full.names=TRUE))
metadata$analysis_version<-paste0(metadata$analysis_version,"-blood-composition-v1");metadata$run_utc<-format(Sys.time(),tz="UTC",usetz=TRUE)
metadata$script_path<-normalizePath(script);metadata$script_sha256<-file_sha(script);metadata$helper_path<-file.path(dirname(script),"bridge_omics_helpers.R");metadata$helper_sha256<-file_sha(metadata$helper_path)
metadata$cell_composition_adjusted<-TRUE;metadata$analysis_role<-"Sensitivity; cell fractions may mediate total dietary response and are estimated, not measured"
metadata$design_formula<-"Paired M/beta changes; original arm contrasts plus changes in CD8T,CD4T,NK,Bcell,Mono; Gran reference fraction omitted"
metadata$cell_estimation<-list(method="minfi estimateCellCounts / Houseman reference projection",reference="FlowSorted.Blood.450k adult blood",reference_version=as.character(packageVersion("FlowSorted.Blood.450k")),referencePlatform="IlluminaHumanMethylation450k",processMethod="auto = preprocessQuantile for blood",probeSelect="both",EPIC_scope="Converted to shared 450K reference probes; EPIC-only loci do not determine cell estimates")
metadata$input_manifest<-lapply(c(path,file.path(input,"paired_change_inputs.rds"),file.path(input,"analysis_metadata.json"),raw_files,reference_files),function(p)list(path=normalizePath(p),sha256=file_sha(p),bytes=file.info(p)$size))
metadata$result_files<-list("cpg_effects.tsv.gz");metadata$auxiliary_files<-list("estimated_cell_proportions.tsv")
write_json(metadata,file.path(output,"analysis_metadata.json"),auto_unbox=TRUE,pretty=TRUE)
unlink(scratch,recursive=TRUE)
cat(toJSON(list(status="completed",people=nrow(design),samples=nrow(samples),joint_fdr_lt_005=sum(result$q_value<.05,na.rm=TRUE)),auto_unbox=TRUE),"\n")
