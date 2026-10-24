# End-to-end CLI exercise using only small synthetic microarray-like input.
args<-commandArgs(trailingOnly=TRUE)
runner<-if(length(args))args[1L] else "scripts/replay_saved_expression_archive.R"
source(file.path(dirname(runner),"expression_archive_helpers.R"))
suppressPackageStartupMessages({library(limma);library(jsonlite)})
set.seed(919721)
folder<-tempfile("saved-expression-replay-");dir.create(folder)
samples<-paste0("S",1:24);info<-data.frame(sample_id=samples,arm=rep(c("A","B","C"),each=8L))
E<-matrix(rnorm(160*24),160,dimnames=list(paste0("gene",1:160),samples))
X<-model.matrix(~0+arm,info);colnames(X)<-c("A","B","C")
C<-makeContrasts(BvsA=B-A,CvsA=C-A,levels=X)
state<-list(expression=E,sample_info=info,design=X,contrasts=C)
input<-file.path(folder,"input.rds");saveRDS(state,input)
metadata<-file.path(folder,"parent.json");write_json(list(accession="GSE13466",analysis_version="synthetic-only-no-human-data"),metadata,auto_unbox=TRUE)
lock<-file.path(folder,"environment.lock");writeLines("Synthetic test lock only",lock)
fit<-eBayes(contrasts.fit(lmFit(E,X),C),robust=TRUE)
q<-matrix(p.adjust(as.vector(fit$p.value),"BH"),nrow=160L)
rows<-lapply(1:2,function(k){se<-fit$stdev.unscaled[,k]*sqrt(fit$s2.post);critical<-qt(.975,fit$df.total)
 data.frame(feature_id=rownames(E),contrast=colnames(C)[k],effect_estimate=fit$coefficients[,k],standard_error=se,
  confidence_low=fit$coefficients[,k]-critical*se,confidence_high=fit$coefficients[,k]+critical*se,
  p_value=fit$p.value[,k],q_value=q[,k],q_value_contrast=p.adjust(fit$p.value[,k],"BH"),q_value_study_family=q[,k])})
reference<-file.path(folder,"reference.tsv.gz");write_ref<-function(rows){con<-gzfile(reference,"wt");on.exit(close(con));write.table(rows,con,sep="\t",quote=FALSE,row.names=FALSE)}
table<-do.call(rbind,rows);write_ref(table)
Rscript<-file.path(R.home("bin"),if(.Platform$OS.type=="windows")"Rscript.exe" else "Rscript")
command<-function(out,expected=expression_archive_sha256(input))c("--vanilla",runner,
 "--source-rds",input,"--expected-sha256",expected,"--parent-metadata",metadata,
 "--expected-parent-sha256",expression_archive_sha256(metadata),"--reference-gene-effects",reference,
 "--expected-reference-sha256",expression_archive_sha256(reference),"--q-scope","study_joint",
 "--environment-lock",lock,"--expected-environment-sha256",expression_archive_sha256(lock),
 "--output-dir",out,"--archive-version","synthetic-archive-test")
run<-function(command){result<-suppressWarnings(system2(Rscript,shQuote(command),stdout=TRUE,stderr=TRUE));status<-attr(result,"status");list(text=result,status=if(is.null(status))0L else status)}
output<-file.path(folder,"out");ok<-run(command(output))
if(ok$status!=0L)stop(paste(ok$text,collapse="\n"))
receipt<-fromJSON(file.path(output,"archive_metadata.json"))
stopifnot(!receipt$scientific_approval,!receipt$original_result_tables_regenerated,!receipt$pathway_statistics_recomputed,
 receipt$gene_statistics_recomputed,receipt$expert_review_status=="pending",file.exists(file.path(output,"reanalysis_inputs.rds")),
 !file.exists(file.path(output,"analysis_metadata.json")))
restored<-readRDS(file.path(output,"reanalysis_inputs.rds"));roundtrip<-refit_expression_model_archive(restored)
stopifnot(isTRUE(all.equal(fit$p.value,roundtrip$moderated_contrast_fit$p.value,tolerance=1e-12)))
stopifnot(run(command(file.path(folder,"bad-hash"),paste(rep("0",64),collapse="")))$status!=0L)
table$effect_estimate[1L]<-table$effect_estimate[1L]+1;write_ref(table)
stopifnot(run(command(file.path(folder,"bad-values")))$status!=0L,!dir.exists(file.path(folder,"bad-values")))
unlink(folder,recursive=TRUE)
cat("Saved normalized-input CLI replay numeric/hash guards passed; no GEO samples processed\n")
