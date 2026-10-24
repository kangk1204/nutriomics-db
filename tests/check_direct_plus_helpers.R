# Small synthetic regression only: never reads public IDAT or changes an effect DB.
args<-commandArgs(trailingOnly=TRUE)
source(if(length(args))args[1L] else "scripts/direct_plus_helpers.R")
suppressPackageStartupMessages(library(limma))
assert_fails<-function(expr)stopifnot(inherits(tryCatch({force(expr);NULL},error=identity),"error"))
set.seed(91271)
folder<-tempfile("direct-plus-test-");dir.create(folder)
probe<-file.path(folder,"hash-source.bin");writeBin(as.raw(0:255),probe)
hash<-dp_file_sha(probe)
stopifnot(identical(class(hash),"character"),length(hash)==1L,nchar(hash)==64L,grepl("^[0-9a-f]{64}$",hash))
normalization_identity<-list(analysis_version="direct-plus-ssnoob-limma-between-arm-v4",script_sha256=hash,
 helper_sha256=dp_file_sha(if(length(args))args[1L] else "scripts/direct_plus_helpers.R"),
 annotation_data=list(list(path=probe,sha256=hash)))
identity_path<-file.path(folder,"normalization_identity.json")
jsonlite::write_json(normalization_identity,identity_path,auto_unbox=TRUE,pretty=TRUE)
decoded<-jsonlite::fromJSON(identity_path,simplifyVector=FALSE)
stopifnot(identical(decoded,normalization_identity),identical(decoded$script_sha256,dp_file_sha(probe)))
people<-paste0("P",seq_len(36L));samples<-paste0("S",seq_len(72L))
info<-data.frame(sample_id=samples,subject_id=rep(people,each=2L),time_months=rep(c(0L,18L),36L),
 arm=rep(rep(c("HDG","MED","greenMED"),each=12L),each=2L),stringsAsFactors=FALSE)
ids<-sprintf("cg%08d",seq_len(203L))
beta<-matrix(runif(203L*72L,.05,.95),203L,dimnames=list(ids,samples));m<-log2(beta/(1-beta))
chunk_indices<-split(seq_len(72L),ceiling(seq_len(72L)/10L))
chunk_paths<-vapply(seq_along(chunk_indices),function(k){
 idx<-chunk_indices[[k]];path<-file.path(folder,paste0("chunk",k,".rds"))
 saveRDS(list(sample_ids=samples[idx],beta=beta[,idx,drop=FALSE],m_values=m[,idx,drop=FALSE],
  detected=matrix(TRUE,203L,length(idx),dimnames=list(ids,samples[idx])),
  sample_detection_fail_fraction=rep(0,length(idx))),path);path},character(1))
qc<-dp_stream_qc(chunk_paths,info)
stopifnot(all(qc$included),all(qc$detected_count==72L),all(qc$detection_pass),all(qc$finite))
altered<-readRDS(chunk_paths[1L]);altered$sample_detection_fail_fraction[1L]<-NA_real_;saveRDS(altered,chunk_paths[1L])
qc_bad<-dp_stream_qc(chunk_paths,info)
stopifnot(sum(!qc_bad$included)==2L,all(info$subject_id[!qc_bad$included]=="P1"))
altered$sample_detection_fail_fraction[1L]<-0;saveRDS(altered,chunk_paths[1L])
tiles<-dp_write_tiles(chunk_paths,samples,ids,file.path(folder,"tiles"),37L)
stopifnot(length(tiles)==6L)
restored_m<-do.call(rbind,lapply(tiles,dp_read_tile,scale="m"))
restored_beta<-do.call(rbind,lapply(tiles,dp_read_tile,scale="beta"))
stopifnot(identical(restored_m,m),identical(restored_beta,beta))
assert_fails(dp_write_tiles(chunk_paths,samples,ids,file.path(folder,"tiles"),37L))
person<-info[seq.int(1L,72L,2L),];rownames(person)<-people
person$baseline_plate<-factor(rep(seq_len(3L),12L));person$followup_plate<-factor(rep(c(2L,3L,1L),12L))
person$baseline_position<-factor(rep(seq_len(4L),9L));person$followup_position<-factor(rep(c(4L,1L,2L,3L),9L))
person$sex<-factor(rep(c("female","male"),18L));person$source_age_unconverted<-seq(30,65,length.out=36L)
primary<-dp_design(person)
technical<-dp_design(person,c("baseline_plate","followup_plate","baseline_position","followup_position"),"technical")
stopifnot(primary$status=="estimable_pending_scientific_review",technical$status=="estimable_pending_scientific_review",
 primary$full_rank==3L,all(unlist(technical$contrast_estimable)),length(technical$dropped_redundant_nuisance)>0L)
confounded<-person;confounded$baseline_plate<-factor(confounded$arm)
stopifnot(dp_design(confounded,"baseline_plate","aliased")$status=="unavailable_nonidentifiable")
missing<-person;missing$source_age_unconverted[1L]<-NA_real_
stopifnot(dp_design(missing,c("source_age_unconverted","sex"))$status=="unavailable_missing_covariates")
baseline<-seq.int(1L,72L,2L);followup<-baseline+1L
for(model in list(primary,technical))for(scale in c("m","beta")){
 full<-if(scale=="m")m else beta
 delta<-full[,followup]-full[,baseline];colnames(delta)<-people
 whole<-eBayes(contrasts.fit(lmFit(delta,model$design),model$contrasts),robust=TRUE)
 paths<-vapply(seq_along(tiles),function(k){
  values<-dp_read_tile(tiles[[k]],scale);change<-values[,followup]-values[,baseline];colnames(change)<-people
  fit<-contrasts.fit(lmFit(change,model$design),model$contrasts)
  fit$checkpoint_key<-"test";path<-file.path(folder,paste0(scale,k,".rds"));saveRDS(fit,path);path},character(1))
 joined<-eBayes(dp_join_fits(paths),robust=TRUE)
 for(field in c("coefficients","stdev.unscaled","sigma","df.residual","s2.post","t","p.value","df.total","s2.prior","df.prior"))
  stopifnot(isTRUE(all.equal(whole[[field]],joined[[field]],tolerance=1e-12)))
 stopifnot(identical(p.adjust(as.vector(whole$p.value),"BH"),p.adjust(as.vector(joined$p.value),"BH")))
 restored_path<-file.path(folder,paste0("fitted_",scale,".rds"));saveRDS(joined,restored_path)
 stopifnot(identical(joined,readRDS(restored_path)))
}
altered<-readRDS(chunk_paths[1L]);colnames(altered$beta)<-rev(colnames(altered$beta));saveRDS(altered,chunk_paths[1L])
assert_fails(dp_stream_qc(chunk_paths,info))
con<-file(tiles[[1L]]$paths$m,"ab");writeBin(1,con);close(con)
assert_fails(dp_read_tile(tiles[[1L]],"m"))
unlink(folder,recursive=TRUE)
cat("DIRECT PLUS synthetic tests passed: sequential tiles, QC pairs, exact global eBayes/FDR, saved models, estimability and corruption guards\\n")
