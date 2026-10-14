# DMRcate exploratory segmentation with fixed parameters and joint-CpG seeds.
script<-sub("^--file=","",grep("^--file=",commandArgs(),value=TRUE)[1])
source(file.path(dirname(script),"bridge_omics_helpers.R"))
suppressPackageStartupMessages(library(GenomicRanges))
input<-normalizePath(arg_value("--input-dir",""),mustWork=TRUE)
output<-arg_value("--output-dir",file.path(input,"dmrcate-sensitivity-v1"));dir.create(output,recursive=TRUE,showWarnings=FALSE)
metadata<-fromJSON(file.path(input,"analysis_metadata.json"),simplifyVector=FALSE)
path<-file.path(input,"cpg_effects.tsv.gz");con<-gzfile(path);header<-strsplit(readLines(con,n=1),"\t")[[1]];close(con)
classes<-setNames(rep("NULL",length(header)),header)
classes[c("feature_id","contrast","chromosome")]<-"character"
classes[c("position","effect_estimate","p_value","q_value","m_value_estimate","m_value_standard_error")]<-"numeric"
tab<-read.delim(gzfile(path),check.names=FALSE,stringsAsFactors=FALSE,colClasses=classes,quote="",comment.char="")
tables<-list();statuses<-list()
for(contrast in unique(tab$contrast)) {
 x<-tab[tab$contrast==contrast,]
 x<-x[grepl("^cg[0-9]+$",x$feature_id)&is.finite(x$position)&x$position>0&grepl("^chr([1-9]|1[0-9]|2[0-2])$",x$chromosome)&is.finite(x$m_value_standard_error)&x$m_value_standard_error>0,]
 sig<-is.finite(x$q_value)&x$q_value<.05
 if(!any(sig)) {statuses[[contrast]]<-list(status="not_tested_no_significant_seed_cpgs",seed_cpgs=0L,threshold="Primary joint study-family CpG BH <0.05; not relaxed");next}
 if(!requireNamespace("DMRcate",quietly=TRUE)){statuses[[contrast]]<-list(status="excluded_dependency_unavailable",seed_cpgs=sum(sig));next}
 gr<-GRanges(x$chromosome,IRanges(as.integer(x$position),width=1),stat=x$m_value_estimate/x$m_value_standard_error,rawpval=x$p_value,diff=x$effect_estimate,ind.fdr=x$q_value,is.sig=sig)
 names(gr)<-x$feature_id;gr<-sort(gr)
 annotation<-methods::new("CpGannotated",ranges=gr)
 called<-tryCatch(DMRcate::dmrcate(annotation,lambda=1000,C=2,min.cpgs=3),error=function(e)e)
 if(inherits(called,"error")){statuses[[contrast]]<-list(status="not_estimable",error=conditionMessage(called),seed_cpgs=sum(sig));next}
 ranges<-tryCatch(DMRcate::extractRanges(called,genome="hg19"),error=function(e)e)
 if(inherits(ranges,"error")){statuses[[contrast]]<-list(status="not_estimable",error=conditionMessage(ranges),seed_cpgs=sum(sig));next}
 raw<-as.data.frame(ranges);write.table(raw,file.path(output,paste0(contrast,"_native_regions.tsv")),sep="\t",quote=FALSE,row.names=FALSE)
 if(!nrow(raw)){statuses[[contrast]]<-list(status="completed_no_regions",seed_cpgs=sum(sig));next}
 overlaps<-findOverlaps(ranges,gr);members<-split(subjectHits(overlaps),queryHits(overlaps))
 native_p<-if("Stouffer"%in%names(raw))raw$Stouffer else rep(NA_real_,nrow(raw))
 tables[[contrast]]<-data.frame(feature_type="dmr",feature_id=paste0(as.character(seqnames(ranges)),":",start(ranges),"-",end(ranges)),
 feature_label="Exploratory DMRcate segmented region",contrast=contrast,tissue="whole blood",timepoint=if(metadata$accession=="GSE107205")"5-year change difference" else "18-month change difference",
 intervention="Diet-arm comparison with joint-FDR-seeded DMRcate spatial smoothing",dose=NA_character_,duration=if(metadata$accession=="GSE107205")"5 years" else "18 months",
 effect_estimate=vapply(seq_len(nrow(raw)),function(i)mean(mcols(gr)$diff[members[[as.character(i)]]]),numeric(1)),standard_error=NA_real_,
 p_value=native_p,q_value=NA_real_,effect_unit="descriptive mean CpG delta delta beta in selected region",status="measured",
 n_people=metadata$independent_people,n_samples=metadata$sample_count,genome_build="hg19",
 metadata_json=vapply(seq_len(nrow(raw)),function(i)as.character(toJSON(list(method="DMRcate",lambda=1000,C=2,min_cpgs=3,
 seed_threshold="Joint study-family CpG BH <0.05",native_region_statistics=as.list(raw[i,,drop=FALSE]),
 multiple_testing_limit="Data-dependent segmentation is exploratory; no calibrated study-wide region BH or post-selection CI claimed; primary calibrated regions are a separate fixed-bin analysis"),auto_unbox=TRUE)),character(1)),stringsAsFactors=FALSE)
 statuses[[contrast]]<-list(status="completed_exploratory",seed_cpgs=sum(sig),regions=nrow(raw))
}
result<-if(length(tables))do.call(rbind,tables) else data.frame(feature_type=character(),feature_id=character(),contrast=character(),p_value=numeric(),q_value=numeric())
write_gzip_tsv(result,file.path(output,"dmr_effects.tsv.gz"));write_json(statuses,file.path(output,"dmr_status.json"),pretty=TRUE,auto_unbox=TRUE)
metadata$analysis_version<-paste0(metadata$analysis_version,"-dmrcate-sensitivity-v1");metadata$run_utc<-format(Sys.time(),tz="UTC",usetz=TRUE)
metadata$script_path<-normalizePath(script);metadata$script_sha256<-file_sha(script);metadata$helper_path<-file.path(dirname(script),"bridge_omics_helpers.R");metadata$helper_sha256<-file_sha(metadata$helper_path)
metadata$analysis_role<-"Exploratory data-dependent DMR segmentation sensitivity; primary CpG and fixed-region tests separate"
metadata$input_manifest<-lapply(c(path,file.path(input,"analysis_metadata.json")),function(p)list(path=p,sha256=file_sha(p),bytes=file.info(p)$size))
metadata$result_files<-list("dmr_effects.tsv.gz");metadata$auxiliary_files<-list("dmr_status.json")
metadata$status<-if(nrow(result))"completed_exploratory" else "completed_no_admissible_results"
metadata$DMRcate_version<-if(requireNamespace("DMRcate",quietly=TRUE))as.character(packageVersion("DMRcate")) else NA_character_
write_json(metadata,file.path(output,"analysis_metadata.json"),pretty=TRUE,auto_unbox=TRUE)
cat(toJSON(list(status=metadata$status,dmr_rows=nrow(result)),auto_unbox=TRUE),"\n")
