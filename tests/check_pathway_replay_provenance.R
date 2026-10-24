# Small synthetic regression only; no GEO/file-system source data or model refits.
args<-commandArgs(trailingOnly=TRUE)
source(if(length(args))args[1L] else 'scripts/pathway_replay_provenance.R')
suppressPackageStartupMessages({library(openssl);library(jsonlite)})
expect_error<-function(x)stopifnot(inherits(tryCatch({force(x);NULL},error=identity),'error'))
set.seed(7123L)
t<-matrix(rnorm(120*2),120,2,dimnames=list(paste0('gene',1:120),c('A','B')))
# Exact tied t values and duplicate Entrez IDs exercise the original stable path.
t[2,1]<-t[1,1]
map<-setNames(as.character(1:120),rownames(t));map[2]<-map[1]
sets<-list(P1=as.character(1:15),P2=as.character(31:50),P3=as.character(72:94))
set.seed(20261001L);state<-.Random.seed;kind<-RNGkind()
a<-pathway_diag_from_actual_t(t,map,sets,state,kind,list(input_certainty='synthetic_only'))
stopifnot(identical(state,.Random.seed),identical(a$raw_moderated_t,t),identical(a$gene_entrez,map),
 !anyDuplicated(names(a$stats[[1L]])),length(a$stats[[1L]])==119L)
# This fallback verifies orchestration/RNG guards; it is never reported as fgsea.
mock<-function(pathways,stats,minSize,maxSize,nproc){
 stopifnot(nproc==1L,minSize==10L,maxSize==500L)
 p<-runif(length(pathways));data.frame(pathway=names(pathways),pval=p,padj=p.adjust(p,'BH'),NES=unname(stats[seq_along(pathways)]))
}
r<-pathway_diag_compare_bundles(a,a,mock)
stopifnot(identical(state,.Random.seed),r$checks$reference_twice$exact_result_objects_identical,
 r$checks$replay_twice$exact_result_objects_identical,r$checks$reference_vs_replay$exact_rng_traces_identical,
 !r$checks$reference_twice$drift_accepted,!r$scientific_approval)
single<-pathway_diag_single_checkpoint(a,mock)
stopifnot(single$checks$same_checkpoint_twice$exact_result_objects_identical,
 single$checks$same_checkpoint_twice$exact_rng_traces_identical,!single$reference_exact_historical_t_available)
b<-a;b$stats[[1L]][1L]<-b$stats[[1L]][1L]+1
expect_error(pathway_diag_validate_bundle(b))
b$exact_stats_sha256<-pathway_diag_object_sha(b$stats)
r2<-pathway_diag_compare_bundles(a,b,mock)
stopifnot(!r2$input_comparison$exact_stats_identical,!r2$checks$reference_vs_replay$exact_result_objects_identical,
 !r2$checks$reference_vs_replay$drift_accepted)
b<-a;b$stats[[1L]]<-rev(b$stats[[1L]]);b$exact_stats_sha256<-pathway_diag_object_sha(b$stats)
expect_error(pathway_diag_validate_bundle(b))
b<-a;b$provenance$input_certainty<-'rounded_logfc_divided_by_se';expect_error(pathway_diag_validate_bundle(b))
b<-a;b$provenance$input_certainty<-'actual_executed_fgsea_inputs';expect_error(pathway_diag_validate_bundle(b))
b<-a;b$scientific_approval<-TRUE;expect_error(pathway_diag_validate_bundle(b))
b<-a;b$initial_seed_state[2L]<-b$initial_seed_state[2L]+1L;expect_error(pathway_diag_validate_bundle(b))
# The old TSV has no exact t; logFC/SE is a sensitivity input, not recovered t.
reference<-do.call(rbind,lapply(1:2,function(k)data.frame(feature_id=rownames(t),contrast=colnames(t)[k],
 effect_estimate=t[,k]*.21,standard_error=.21)))
proxy<-pathway_diag_reported_t_proxy(reference,t)
stopifnot(identical(dimnames(proxy),dimnames(t)),max(abs(proxy-t))<1e-12)
bad_reference<-reference;bad_reference$standard_error[1L]<-0
expect_error(pathway_diag_reported_t_proxy(bad_reference,t))
b<-a;b$provenance$input_certainty<-'reported_tsv_precision_proxy'
b$provenance$source_artifacts<-list(list(path='synthetic',role='t_or_executed_stats',sha256=paste(rep('0',64),collapse='')))
expect_error(pathway_diag_compare_bundles(b,a,mock))
sensitivity<-pathway_diag_compare_bundles(b,a,mock,allow_reported_precision_proxy=TRUE)
stopifnot(!sensitivity$reference_exact_historical_t_available,
 sensitivity$method_version=='pathway-loop-replay-reported-precision-sensitivity-v1',!sensitivity$scientific_approval)
expect_error(pathway_diag_single_checkpoint(b,mock))
real_fgsea<-requireNamespace('fgsea',quietly=TRUE)
if(real_fgsea) {
 r<-pathway_diag_compare_bundles(a,a,fgsea::fgsea)
 stopifnot(r$checks$reference_twice$exact_result_objects_identical,r$checks$replay_twice$exact_result_objects_identical,
   r$checks$reference_vs_replay$exact_result_objects_identical,r$checks$reference_vs_replay$exact_rng_traces_identical)
 one<-pathway_diag_single_checkpoint(a,fgsea::fgsea)
 stopifnot(one$checks$same_checkpoint_twice$exact_result_objects_identical,one$checks$same_checkpoint_twice$exact_rng_traces_identical)
 # Exercise the actual single-checkpoint CLI in a new temporary output only.
 fixture<-tempfile('fgsea-synthetic-cli-');dir.create(fixture)
 input<-file.path(fixture,'synthetic-checkpoint.rds');saveRDS(a,input,version=3,compress=FALSE)
 binary<-file.path(R.home('bin'),if(.Platform$OS.type=='windows')'Rscript.exe' else 'Rscript')
 runner<-if(length(args))args[1L] else 'scripts/pathway_replay_provenance.R'
 command<-c('--vanilla',runner,'--checkpoint-bundle',input,'--expected-checkpoint-sha256',pathway_diag_sha(input),
   '--output-dir',file.path(fixture,'diagnostic'))
 output<-suppressWarnings(system2(binary,shQuote(command),stdout=TRUE,stderr=TRUE,timeout=30))
 if(!is.null(attr(output,'status'))&&attr(output,'status')!=0L)stop(paste(output,collapse='\n'))
 receipt<-fromJSON(file.path(fixture,'diagnostic','diagnostic-receipt.json'))
 stopifnot(receipt$checks$same_checkpoint_twice$exact_result_objects_identical,
  !receipt$reference_exact_historical_t_available,!receipt$scientific_approval,
  receipt$raw_results_file=='two-loop-raw-results.rds')
 unlink(fixture,recursive=TRUE)
}
# Lossless plain-character SHA and seed JSON receipt, no S3 hash leakage.
p<-tempfile(fileext='.rds');saveRDS(a,p,compress=FALSE,version=3)
h<-pathway_diag_sha(p);stopifnot(identical(class(h),'character'),nchar(h)==64L)
j<-toJSON(list(sha256=h,initial_seed_state=state),auto_unbox=TRUE)
stopifnot(identical(fromJSON(j)$initial_seed_state,state));unlink(p)
cat(toJSON(list(synthetic_only=TRUE,actual_fgsea_synthetic_exercised=real_fgsea,scientific_approval=FALSE,
 guard_cases=12L,full_loop_reset_not_per_contrast=TRUE,no_real_study_executed=TRUE),auto_unbox=TRUE),'\n')
