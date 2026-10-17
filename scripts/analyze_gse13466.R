script<-sub("^--file=","",grep("^--file=",commandArgs(),value=TRUE)[1]); source(file.path(dirname(script),"bridge_omics_helpers.R"))
root<-arg_value("--root",Sys.getenv("NUTRIOMICS_ANALYSIS_ROOT", unset="expansion"))
raw<-file.path(root,"raw","GSE13466"); output<-arg_value("--output-dir",file.path(root,"outputs","gse13466-processed-v1"))
dir.create(output,recursive=TRUE,showWarnings=FALSE)
records<-fromJSON(file.path(raw,"sample_metadata.json"),simplifyVector=FALSE)
titles<-vapply(records,function(x)x$title[[1]],character(1))
parsed<-regmatches(titles,regexec("^PBMC ([0-9]+) (PUFA|SFA) (baseline|6 h after intake)$",titles))
if(length(records)!=84L || any(lengths(parsed)!=4L)) stop("Unexpected design; audit titles")
sample_info<-data.frame(sample_accession=vapply(records,function(x)x$sample_accession,character(1)),
 subject_id=vapply(parsed,function(x)x[2],character(1)),arm=vapply(parsed,function(x)x[3],character(1)),
 timepoint=ifelse(vapply(parsed,function(x)x[4],character(1))=="baseline","0h","6h"))
if(length(unique(sample_info$subject_id))!=21L || any(table(sample_info$subject_id,sample_info$arm,sample_info$timepoint)!=1L)) stop("Incomplete crossover pairs")
expression<-read_geo_matrix(file.path(raw,"GSE13466_series_matrix.txt.gz"))
expression<-expression[,sample_info$sample_accession,drop=FALSE]
raw_mode<-arg_value("--raw-cdf","false")=="true"
if(raw_mode) {
  suppressPackageStartupMessages({library(affy);library(makecdfenv)})
  cel_root<-file.path(root,"processed","GSE13466","raw_files");dir.create(cel_root,recursive=TRUE,showWarnings=FALSE)
  archive<-file.path(raw,"GSE13466_RAW.tar");members<-untar(archive,list=TRUE)
  if(any(grepl("(^/|(^|/)\\.\\.(/|$))",members)))stop("Unsafe tar paths")
  untar(archive,exdir=cel_root)
  cdf_gz<-file.path(raw,"GPL7144_HsNuGOHs1a520180_Hs_ENTREZG.cdf.gz")
  cdf_file<-sub("\\.gz$","",cdf_gz)
  inflate<-function(a,b){input<-gzfile(a,"rb");output_connection<-file(b,"wb");on.exit(close(input));on.exit(close(output_connection),add=TRUE);repeat{block<-readBin(input,"raw",1024L*1024L);if(!length(block))break;writeBin(block,output_connection)}}
  if(!file.exists(cdf_file))inflate(cdf_gz,cdf_file)
  cdf_env<-make.cdf.env(basename(cdf_file),cdf.path=dirname(cdf_file),verbose=TRUE)
  assign("bridgecustomnugo",cdf_env,envir=.GlobalEnv)
  bioc_options<-getOption("BioC");bioc_options$affy$probesloc<-list(list(what="environment",where=.GlobalEnv));options(BioC=bioc_options)
  cel_files<-list.files(cel_root,pattern="[.]CEL[.]gz$",recursive=TRUE,full.names=TRUE)
  gsm<-sub("[.].*$","",basename(cel_files));matching<-match(sample_info$sample_accession,gsm)
  if(anyNA(matching)||anyDuplicated(gsm)||length(cel_files)!=84L)stop("CEL-GSM mapping incomplete")
  inflated<-sub("[.]gz$","",cel_files[matching])
  for(i in seq_along(inflated))if(!file.exists(inflated[i]))inflate(cel_files[matching][i],inflated[i])
  batch<-ReadAffy(filenames=inflated,sampleNames=sample_info$sample_accession,cdfname="bridgecustomnugo")
  raw_qc<-data.frame(sample_accession=sample_info$sample_accession,mean_pm=colMeans(pm(batch)),median_pm=apply(pm(batch),2,median))
  write.table(raw_qc,file.path(output,"raw_cel_qc.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
  expression<-exprs(rma(batch,verbose=TRUE));rm(batch);gc()
}
entrez<-sub("_at$","",rownames(expression))
valid<-grepl("^[0-9]+$",entrez)
write.table(data.frame(source_id=rownames(expression),entrez_id=ifelse(valid,entrez,NA_character_),included=valid,
 reason=ifelse(valid,"NuGO Entrez custom CDF identifier","Control/non-Entrez feature excluded")),
 file.path(output,"feature_inclusion.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
if(sum(valid)<10000L) stop("NuGO Hs_ENTREZG_10 identifiers not recognized")
expression<-expression[valid,,drop=FALSE]; entrez<-entrez[valid]
labels<-unname(mapIds(org.Hs.eg.db,keys=unique(entrez),column="SYMBOL",keytype="ENTREZID",multiVals="first")[entrez])
labels[is.na(labels)]<-entrez[is.na(labels)]
write.table(data.frame(platform="GPL7144",source_id=rownames(expression),entrez_id=entrez,gene_symbol=labels,
 mapping="Published NuGO custom CDF Hs_ENTREZG_10 Entrez identifiers; no standard Affymetrix annotation substituted"),
 file.path(output,"feature_crosswalk.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
rownames(expression)<-entrez
sample_info$subject_id<-factor(sample_info$subject_id)
sample_info$condition<-factor(paste(sample_info$arm,sample_info$timepoint,sep="_"))
design<-model.matrix(~0+condition+subject_id,sample_info); colnames(design)<-sub("^condition","",colnames(design))
contrasts<-makeContrasts(PUFA_vs_SFA_6h_change=(PUFA_6h-PUFA_0h)-(SFA_6h-SFA_0h),levels=design)
export_expression(expression,sample_info,design,contrasts,"GSE13466",if(raw_mode)"gse13466-rma-customcdf-limma-paired-v2" else "gse13466-limma-paired-processed-v1",output,entrez,labels,
 context=list(tissue="PBMC",timepoint=list("6h change difference"),dose=list(NA_character_),duration=list("acute"),
 intervention="PUFA shake versus SFA shake",normalization=if(raw_mode)"Independent CEL RMA using the exact deposited GPL7144 NuGO Hs_ENTREZG_10 custom CDF" else "Depositor RMA processed matrix; no second log transform or quantile normalization",
 interpretation="21 independent people; paired shake difference in 6h changes. PBMC differs from whole blood. Does not isolate individual fatty acids or predict personal health."),
 input_files=c(if(raw_mode)c(file.path(raw,"GSE13466_RAW.tar"),file.path(raw,"GPL7144_HsNuGOHs1a520180_Hs_ENTREZG.cdf.gz")) else file.path(raw,"GSE13466_series_matrix.txt.gz"),file.path(raw,"sample_metadata.json")),
 qc_limit=if(raw_mode)"Independent CEL RMA and PM intensity quantile QC; no blood-cell composition estimates or independent RNA integrity measurements." else "Processed-matrix quantile QC only. Raw CEL preserved separately; original custom-CDF package required for independent raw RMA reproduction.")
