# Synthetic only: no minfi/IDAT/reference estimation is executed in this test.
args<-commandArgs(trailingOnly=TRUE)
source(if(length(args))args[1L] else "scripts/direct_plus_helpers.R")
source(if(length(args)>1L)args[2L] else "scripts/analyze_direct_plus_blood_sensitivity.R")
suppressPackageStartupMessages(library(limma))
expect_error<-function(expr,code=NULL) {
 error<-tryCatch({force(expr);NULL},error=identity)
 stopifnot(inherits(error,"error"));if(!is.null(code))stopifnot(identical(error$code,code))
 invisible(error)
}
stopifnot(dpb_mem_available(c("MemTotal: 999999999 kB","MemAvailable: 83886080 kB"))==80)
stopifnot(dpb_memory_gate(80)$reserve_gib==24)
expect_error(dpb_memory_gate(75),"resource_insufficient")
expect_error(dpb_memory_gate(80,reserve=23),"resource_unavailable")
expect_error(dpb_mem_available("MemTotal: 83886080 kB"),"resource_unavailable")
set.seed(198426)
people<-paste0("P",seq_len(36L));samples<-paste0("S",seq_len(72L))
types<-c("CD8T","CD4T","NK","Bcell","Mono","Gran")
raw<-matrix(runif(72L*6L,.01,.5),72L,dimnames=list(samples,types))
sample_info<-data.frame(sample_id=samples,subject_id=rep(people,each=2L),time_months=rep(c(0L,18L),36L),
 arm=rep(rep(c("HDG","MED","greenMED"),each=12L),each=2L),stringsAsFactors=FALSE)
person_info<-sample_info[seq.int(1L,72L,2L),];rownames(person_info)<-people
closed<-dpb_close_counts(raw[rev(samples),rev(types)],samples)
stopifnot(identical(rownames(closed$closed),samples),identical(colnames(closed$closed),types),
 max(abs(rowSums(closed$closed)-1))<1e-12)
tiny_negative<-raw;tiny_negative[1L,1L]<- -1e-13
stopifnot(dpb_close_counts(tiny_negative,samples)$roundoff_negative_count==1L)
invalid<-raw;invalid[1L,1L]<- -1e-5
expect_error(dpb_close_counts(invalid,samples),"invalid_cell_estimates")
invalid<-raw;invalid[1L,]<-0
expect_error(dpb_close_counts(invalid,samples),"invalid_cell_estimates")
expect_error(dpb_close_counts(raw[-1L,],samples),"invalid_cell_estimates")
model<-dpb_cell_design(person_info,sample_info,closed$closed)
stopifnot(model$model$status=="estimable_pending_scientific_review",model$model$full_rank==8L,
 identical(colnames(model$delta_counts),paste0("delta_",types[1:5])),
 !"delta_Gran"%in%colnames(model$model$design),nrow(model$model$design)==36L,
 ncol(model$model$contrasts)==3L,all(unlist(model$model$contrast_estimable)))
expect_error(dpb_cell_design(person_info,sample_info,closed$closed[rev(samples),]),"sample_mapping_error")
incomplete<-sample_info;incomplete$time_months[1L]<-18L
expect_error(dpb_cell_design(person_info,incomplete,closed$closed),"sample_mapping_error")
# Five changes that encode arm deterministically must NOT be called identifiable.
confounded<-matrix(0,72L,6L,dimnames=list(samples,types));confounded[,"Gran"]<-1
confounded[sample_info$time_months==18L&sample_info$arm=="MED","CD8T"]<-.1
confounded[sample_info$time_months==18L&sample_info$arm=="greenMED","CD8T"]<-.2
confounded[,"Gran"]<-1-confounded[,"CD8T"]
stopifnot(dpb_cell_design(person_info,sample_info,confounded)$model$status=="unavailable_nonidentifiable")
# Identical global moderation with a nonorthogonal, five-fraction-adjusted design.
E<-matrix(rnorm(203L*36L),203L,dimnames=list(sprintf("cg%08d",1:203),people))
E[1:7,]<-E[1:7,]+matrix(rep(model$delta_counts[,1L]*3,each=7L),7L)
whole<-eBayes(contrasts.fit(lmFit(E,model$model$design),model$model$contrasts),robust=TRUE)
folder<-tempfile("blood-sensitivity-test-");dir.create(folder)
groups<-split(seq_len(nrow(E)),ceiling(seq_len(nrow(E))/37L))
paths<-vapply(seq_along(groups),function(k){
 fit<-contrasts.fit(lmFit(E[groups[[k]],,drop=FALSE],model$model$design),model$model$contrasts)
 fit$checkpoint_key<-"synthetic-blood";path<-file.path(folder,paste0(k,".rds"));saveRDS(fit,path);path},character(1))
joined<-eBayes(dp_join_fits(paths),robust=TRUE)
for(field in c("coefficients","stdev.unscaled","sigma","s2.post","df.residual","df.total","t","p.value"))
 stopifnot(isTRUE(all.equal(whole[[field]],joined[[field]],tolerance=1e-12)))
stopifnot(identical(p.adjust(as.vector(whole$p.value),"BH"),p.adjust(as.vector(joined$p.value),"BH")))
saveRDS(joined,file.path(folder,"fitted.rds"));stopifnot(identical(joined,readRDS(file.path(folder,"fitted.rds"))))
unlink(folder,recursive=TRUE)
cat("DIRECT PLUS blood sensitivity synthetic tests passed; no IDAT, reference normalization or scientific approval executed\n")
