# Diagnostic method pathway-loop-replay-20261022-v1. Sourcing fits/enriches nothing.
pathway_diag_sha <- function(path) {
  con<-file(path,'rb');on.exit(close(con))
  paste0(as.character(openssl::sha256(con)),collapse='')
}
# Version 2 materializes ALTREP values, so cache materialization cannot change the
# value-content digest between validation calls on R 4.5. Exact doubles are kept.
pathway_diag_object_sha <- function(x)paste0(as.character(openssl::sha256(serialize(x,NULL,version=2))),collapse='')
pathway_diag_rng_restore <- function(kind,state) {
  do.call(RNGkind,as.list(kind))
  assign('.Random.seed',state,envir=.GlobalEnv)
}
pathway_diag_preserve_rng <- function() {
  list(kind=RNGkind(),has_seed=exists('.Random.seed',envir=.GlobalEnv,inherits=FALSE),
    seed=if(exists('.Random.seed',envir=.GlobalEnv,inherits=FALSE))get('.Random.seed',envir=.GlobalEnv) else NULL)
}
pathway_diag_return_rng <- function(old) {
  do.call(RNGkind,as.list(old$kind))
  if(old$has_seed)assign('.Random.seed',old$seed,envir=.GlobalEnv)
  else if(exists('.Random.seed',envir=.GlobalEnv,inherits=FALSE))rm('.Random.seed',envir=.GlobalEnv)
}
pathway_diag_validate_bundle <- function(x) {
  if(!identical(x$schema,'nutriomics-exact-fgsea-loop-input-v1'))stop('Unknown exact input schema')
  if(!is.list(x$stats)||!length(x$stats)||length(x$stats)>64L||is.null(names(x$stats))||
     anyDuplicated(names(x$stats))||any(!nzchar(names(x$stats))))stop('Contrast order/identity missing')
  for(v in x$stats)if(!is.numeric(v)||is.null(names(v))||length(v)<10L||length(v)>100000L||
    any(!is.finite(v))||anyDuplicated(names(v))||any(!nzchar(names(v)))||
    !identical(v,sort(v,decreasing=TRUE)))stop('Exact ordered named stats required; no rounded TSV reconstruction')
  if(!is.list(x$pathways)||!length(x$pathways)||length(x$pathways)>10000L||is.null(names(x$pathways))||
     anyDuplicated(names(x$pathways)))stop('Actual ordered pathway mapping required')
  if(any(!vapply(x$pathways,function(v)is.character(v)&&!anyNA(v),logical(1))))stop('Pathway membership must retain original character IDs')
  if(length(x$rng_kind)!=3L||!is.integer(x$initial_seed_state)||length(x$initial_seed_state)<2L||
     !identical(x$seed,20261001L))stop('Historical RNGkind and complete initial state required')
  if(length(x$provenance$input_certainty)!=1L||
     !x$provenance$input_certainty%in%c('actual_executed_fgsea_inputs','actual_saved_model_t_and_hash_verified_mapping','reported_tsv_precision_proxy','synthetic_only'))
    stop('Reference exact input is unavailable/approximate; no synthetic reconstruction as historical proof')
  if(x$provenance$input_certainty!='synthetic_only') {
    sources<-x$provenance$source_artifacts
    if(!is.list(sources)||!length(sources)||any(!vapply(sources,function(s)
      length(s$path)==1L&&length(s$role)==1L&&length(s$sha256)==1L&&grepl('^[a-f0-9]{64}$',s$sha256),logical(1))))
      stop('Exact executed/saved t, mapping, producer and environment artifacts need explicit paths/roles/SHA')
  }
  if(!identical(x$exact_stats_sha256,pathway_diag_object_sha(x$stats))||
     !identical(x$exact_pathways_sha256,pathway_diag_object_sha(x$pathways)))stop('Input bundle content SHA disagrees')
  if(isTRUE(x$scientific_approval)||x$expert_review_status!='pending')stop('No human approval may be generated')
  old<-pathway_diag_preserve_rng();on.exit(pathway_diag_return_rng(old))
  do.call(RNGkind,as.list(x$rng_kind));set.seed(x$seed)
  if(!identical(get('.Random.seed',envir=.GlobalEnv),x$initial_seed_state))
    stop('Initial state is not the declared loop seed/RNGkind; cannot reset per contrast silently')
  invisible(TRUE)
}
pathway_diag_make_bundle <- function(stats,pathways,initial_seed_state,rng_kind,provenance,
    raw_moderated_t=NULL,gene_entrez=NULL,rank_traces=NULL) {
  x<-list(schema='nutriomics-exact-fgsea-loop-input-v1',method_version='pathway-loop-replay-20261022-v1',
    stats=stats,pathways=pathways,contrast_order=names(stats),seed=20261001L,
    initial_seed_state=initial_seed_state,rng_kind=rng_kind,
    raw_moderated_t=raw_moderated_t,gene_entrez=gene_entrez,rank_traces=rank_traces,provenance=provenance,
    exact_stats_sha256=pathway_diag_object_sha(stats),exact_pathways_sha256=pathway_diag_object_sha(pathways),
    scientific_approval=FALSE,expert_review_status='pending')
  pathway_diag_validate_bundle(x);x
}
# Use ONLY actual in-memory/saved moderated t and source-bound mapping. A logFC/SE
# ratio from rounded TSV is explicitly not an exact historical moderated t vector.
pathway_diag_from_actual_t <- function(t_matrix,gene_entrez,pathways,initial_seed_state,rng_kind,provenance) {
  if(!is.matrix(t_matrix)||!is.numeric(t_matrix)||is.null(rownames(t_matrix))||is.null(colnames(t_matrix))||
     !identical(names(gene_entrez),rownames(t_matrix)))stop('Actual t/mapping axes must match exactly')
  traces<-lapply(seq_len(ncol(t_matrix)),function(k){
    valid<-!is.na(gene_entrez)&nzchar(gene_entrez)&is.finite(t_matrix[,k])
    ranks<-data.frame(entrez=as.character(gene_entrez[valid]),t=t_matrix[valid,k],symbol=rownames(t_matrix)[valid])
    ranks<-ranks[order(-abs(ranks$t),ranks$entrez,ranks$symbol),,drop=FALSE]
    ranks[!duplicated(ranks$entrez),,drop=FALSE]
  });names(traces)<-colnames(t_matrix)
  stats<-lapply(traces,function(ranks)sort(setNames(ranks$t,ranks$entrez),decreasing=TRUE))
  pathway_diag_make_bundle(stats,pathways,initial_seed_state,rng_kind,provenance,t_matrix,gene_entrez,traces)
}
pathway_diag_reported_t_proxy <- function(reference,t_template) {
  required<-c('feature_id','contrast','effect_estimate','standard_error')
  if(!all(required%in%names(reference)))stop('Reported logFC/SE source fields missing')
  keys<-paste(reference$feature_id,reference$contrast,sep='\r')
  if(anyDuplicated(keys)||nrow(reference)!=length(t_template))stop('Reported reference keys incomplete/duplicated')
  proxy<-t_template
  for(k in seq_len(ncol(proxy))) {
    index<-match(paste(rownames(proxy),colnames(proxy)[k],sep='\r'),keys)
    if(anyNA(index))stop('Reported reference differs from actual fit feature/contrast keys')
    estimate<-as.numeric(reference$effect_estimate[index]);se<-as.numeric(reference$standard_error[index])
    if(any(!is.finite(estimate))||any(!is.finite(se))||any(se<=0))stop('Non-estimable reported estimate/SE; no fallback to zero')
    proxy[,k]<-estimate/se
  }
  proxy
}
pathway_diag_environment <- function() {
  packages<-c('fgsea','BiocParallel','limma','data.table','Rcpp','BH','reactome.db','hgu219.db','openssl','jsonlite')
  versions<-setNames(lapply(packages,function(p)if(requireNamespace(p,quietly=TRUE))as.character(packageVersion(p)) else NA_character_),packages)
  implementations<-lapply(c('fgsea','fgseaMultilevel','fgseaSimpleImpl','preparePathwaysAndStats','setUpBPPARAM','multilevelImpl'),function(n){
    if(!requireNamespace('fgsea',quietly=TRUE)||!exists(n,asNamespace('fgsea'),inherits=FALSE))return(NULL)
    f<-get(n,asNamespace('fgsea'));body<-paste(deparse(body(f)),collapse='\n')
    list(name=n,formals=paste(deparse(formals(f)),collapse='\n'),body=body,body_sha256=pathway_diag_object_sha(body))
  })
  list(R=as.character(getRversion()),package_versions=versions,implementation=implementations,
    session_info=paste(capture.output(sessionInfo()),collapse='\n'),
    thread_environment=as.list(Sys.getenv(c('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','R_THREADS'))),
    scope='Current diagnostic process; not evidence of unrecorded historical package/RNG state')
}
pathway_diag_one_loop <- function(bundle,engine) {
  pathway_diag_validate_bundle(bundle)
  old<-pathway_diag_preserve_rng();on.exit(pathway_diag_return_rng(old))
  pathway_diag_rng_restore(bundle$rng_kind,bundle$initial_seed_state)
  results<-vector('list',length(bundle$stats));names(results)<-names(bundle$stats)
  for(k in seq_along(results)) {
    before<-get('.Random.seed',envir=.GlobalEnv)
    warnings<-character()
    result<-withCallingHandlers(engine(pathways=bundle$pathways,stats=bundle$stats[[k]],minSize=10L,maxSize=500L,nproc=1L),
      warning=function(w){warnings<<-c(warnings,conditionMessage(w));invokeRestart('muffleWarning')})
    results[[k]]<-list(result=as.data.frame(result),rng_before=before,
      rng_after=get('.Random.seed',envir=.GlobalEnv),warnings=warnings)
  }
  results
}
pathway_diag_compare_runs <- function(a,b) {
  list(exact_result_objects_identical=identical(lapply(a,`[[`,'result'),lapply(b,`[[`,'result')),
    exact_rng_traces_identical=identical(lapply(a,function(x)x[c('rng_before','rng_after')]),
      lapply(b,function(x)x[c('rng_before','rng_after')])),drift_accepted=FALSE,no_tolerance_applied=TRUE)
}
pathway_diag_compare_bundles <- function(a,b,engine,allow_reported_precision_proxy=FALSE) {
  pathway_diag_validate_bundle(a);pathway_diag_validate_bundle(b)
  proxy_used<-any(c(a$provenance$input_certainty,b$provenance$input_certainty)=='reported_tsv_precision_proxy')
  if(proxy_used&&!isTRUE(allow_reported_precision_proxy))stop('Reported-precision proxy requires an explicit diagnostic-only opt-in')
  if(!identical(names(a$stats),names(b$stats)))stop('Original loop contrast order differs')
  if(!identical(a$rng_kind,b$rng_kind)||!identical(a$initial_seed_state,b$initial_seed_state))
    stop('Different captured loop RNG states; do not relabel a per-contrast/new RNG method as historical replay')
  ref1<-pathway_diag_one_loop(a,engine);ref2<-pathway_diag_one_loop(a,engine)
  new1<-pathway_diag_one_loop(b,engine);new2<-pathway_diag_one_loop(b,engine)
  list(reference_run1=ref1,reference_run2=ref2,replay_run1=new1,replay_run2=new2,
    checks=list(reference_twice=pathway_diag_compare_runs(ref1,ref2),replay_twice=pathway_diag_compare_runs(new1,new2),
      reference_vs_replay=pathway_diag_compare_runs(ref1,new1)),
    input_comparison=list(exact_stats_identical=identical(a$stats,b$stats),
      exact_stats_order_identical=identical(lapply(a$stats,names),lapply(b$stats,names)),
      exact_pathways_identical=identical(a$pathways,b$pathways)),
    method_version=if(proxy_used)'pathway-loop-replay-reported-precision-sensitivity-v1' else 'pathway-loop-replay-20261022-v1',
    reference_exact_historical_t_available=isTRUE(a$provenance$historical_original_checkpoint)&&!proxy_used,
    rng_policy='Reset once to the captured shared initial full-loop state for each replicate; never reset at individual contrast',
    scientific_approval=FALSE,expert_review_status='pending',existing_public_results_modified=FALSE)
}
pathway_diag_single_checkpoint <- function(bundle,engine) {
  pathway_diag_validate_bundle(bundle)
  if(bundle$provenance$input_certainty=='reported_tsv_precision_proxy')stop('Single checkpoint mode requires actual full-precision inputs')
  first<-pathway_diag_one_loop(bundle,engine);second<-pathway_diag_one_loop(bundle,engine)
  list(checkpoint_run1=first,checkpoint_run2=second,
    checks=list(same_checkpoint_twice=pathway_diag_compare_runs(first,second)),
    input_comparison=list(scope='One exact full-precision checkpoint; historical result reproduction not tested'),
    method_version='pathway-single-checkpoint-replay-20261022-v1',
    reference_exact_historical_t_available=FALSE,
    scientific_approval=FALSE,expert_review_status='pending',existing_public_results_modified=FALSE)
}
pathway_diag_source_proofs <- function(bundle) {
  sources<-bundle$provenance$source_artifacts
  if(bundle$provenance$input_certainty=='synthetic_only')return(list())
  required<-c('t_or_executed_stats','gene_to_entrez_mapping','pathway_mapping','producer','environment')
  if(!all(required%in%vapply(sources,`[[`,character(1),'role'))||length(sources)>32L)
    stop('Lossless t/stats, gene/pathway mapping, producer and environment proof are all required')
  lapply(sources,function(s){
    info<-file.info(s$path)
    if(is.na(info$size)||info$size>512*1024^2)stop('Missing/oversized declared source proof')
    if(pathway_diag_sha(s$path)!=s$sha256)stop('Upstream source hash mismatch: ',s$role)
    list(path=s$path,role=s$role,bytes=info$size,mtime=as.numeric(info$mtime),sha256=s$sha256)
  })
}
pathway_diag_main <- function(args=commandArgs(trailingOnly=TRUE)) {
  suppressPackageStartupMessages({library(fgsea);library(jsonlite);library(openssl)})
  arg<-function(n){k<-match(n,args);if(is.na(k)||k==length(args))stop('Missing ',n);args[[k+1L]]}
  optional<-function(n,default){k<-match(n,args);if(is.na(k))default else arg(n)}
  for(n in c('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','R_THREADS'))
    if(Sys.getenv(n)!='1')stop('Set all documented thread environment variables to 1 before launch')
  single<-'--checkpoint-bundle'%in%args
  paths<-if(single)c(checkpoint=arg('--checkpoint-bundle')) else c(reference=arg('--reference-bundle'),replay=arg('--replay-bundle'))
  expected<-if(single)c(checkpoint=arg('--expected-checkpoint-sha256')) else c(reference=arg('--expected-reference-sha256'),replay=arg('--expected-replay-sha256'))
  before<-lapply(paths,function(p){i<-file.info(p);if(is.na(i$size)||i$size>256*1024^2)stop('Missing/oversized input bundle');list(size=i$size,mtime=as.numeric(i$mtime),sha256=pathway_diag_sha(p))})
  if(!all(vapply(seq_along(before),function(i)before[[i]]$sha256==expected[i],logical(1))))stop('Exact bundle hash mismatch')
  output<-arg('--output-dir');if(file.exists(output))stop('Use a new immutable diagnostic output directory')
  bundles<-lapply(paths,readRDS)
  lapply(bundles,pathway_diag_validate_bundle)
  upstream_before<-lapply(bundles,pathway_diag_source_proofs)
  script_path<-normalizePath(sub('^--file=','',grep('^--file=',commandArgs(),value=TRUE)[1L]),mustWork=TRUE)
  script_sha<-pathway_diag_sha(script_path)
  result<-if(single)pathway_diag_single_checkpoint(bundles[[1L]],fgsea::fgsea) else
    pathway_diag_compare_bundles(bundles[[1L]],bundles[[2L]],fgsea::fgsea,
    allow_reported_precision_proxy=identical(optional('--allow-reported-precision-proxy','false'),'true'))
  for(k in seq_along(paths))if(pathway_diag_sha(paths[k])!=expected[k])stop('Source changed during replay')
  upstream_after<-lapply(bundles,pathway_diag_source_proofs)
  if(!identical(upstream_before,upstream_after)||pathway_diag_sha(script_path)!=script_sha)
    stop('Upstream source/script fingerprint changed during replay')
  dir.create(output,recursive=TRUE)
  for(k in seq_along(paths)) {
    destination<-file.path(output,paste0(names(paths)[k],'-exact-input.rds'))
    if(!file.copy(paths[k],destination)||pathway_diag_sha(destination)!=expected[k])stop('Lossless input preservation failed')
  }
  result_name<-if(single)'two-loop-raw-results.rds' else 'four-loop-raw-results.rds'
  saveRDS(result,file.path(output,result_name),compress=FALSE,version=3)
  summary<-list(schema='nutriomics-fgsea-loop-diagnostic-receipt-v1',method_version=result$method_version,
    source_inputs=before,environment=pathway_diag_environment(),checks=result$checks,input_comparison=result$input_comparison,
    script_path=script_path,script_sha256=script_sha,upstream_sources=upstream_before,upstream_sources_unchanged=TRUE,
    raw_results_file=result_name,raw_results_sha256=pathway_diag_sha(file.path(output,result_name)),
    existing_public_results_modified=FALSE,scientific_approval=FALSE,expert_review_status='pending',
    reference_exact_historical_t_available=result$reference_exact_historical_t_available,
    note='Diagnostic comparison only; no tolerance, original replacement, causal inference or expert approval')
  write_json(summary,file.path(output,'diagnostic-receipt.json'),auto_unbox=TRUE,pretty=TRUE,null='null',na='null')
  cat(if(single)'Two same-checkpoint' else 'Four comparison','full-loop diagnostic replicates completed; original results remain untouched\n')
}
if(sys.nframe()==0L)pathway_diag_main()
