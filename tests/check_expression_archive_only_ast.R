# Removing only declared archive additions must recover the entire original AST.
args<-commandArgs(trailingOnly=TRUE)
old<-as.list(parse(args[1L],keep.source=FALSE));current<-as.list(parse(args[2L],keep.source=FALSE))
lhs<-function(x)if(is.call(x)&&identical(x[[1L]],as.name("<-"))&&is.symbol(x[[2L]]))as.character(x[[2L]]) else ""
added<-c("archive_script_argument","archive_helper_path","unmoderated_fit","model_archive","archive_contrasts")
current<-Filter(function(x){
 if(lhs(x)%in%added)return(FALSE)
 if(is.call(x)&&identical(x[[1L]],as.name("source"))&&identical(x[[2L]],as.name("archive_helper_path")))return(FALSE)
 TRUE
},current)
original_metadata<-Filter(function(x)lhs(x)=="metadata",old)[[1L]][[3L]]
original_fields<-names(as.list(original_metadata))
for(k in seq_along(current)) {
 node<-current[[k]]
 if(lhs(node)=="metadata") {
  fields<-as.list(node[[3L]]);keep<-!names(fields)%in%c("model_archive","helper_path","helper_sha256")
  if(!"auxiliary_files"%in%original_fields)keep[names(fields)=="auxiliary_files"]<-FALSE
  else {
   index<-match("auxiliary_files",names(fields));aux<-as.list(fields[[index]])
   aux<-Filter(function(x)!identical(x,"reanalysis_inputs.rds"),aux);fields[[index]]<-as.call(aux)
  }
  node[[3L]]<-as.call(fields[keep]);current[[k]]<-node
 }
 if(lhs(node)=="fit"&&identical(node[[3L]],quote(eBayes(unmoderated_fit,robust=TRUE)))) {
  node[[3L]]<-quote(eBayes(lmFit(gene_m,design),robust=TRUE));current[[k]]<-node
 }
}
if(!identical(old,current)) {
 for(k in seq_len(min(length(old),length(current))))if(!identical(old[[k]],current[[k]])) {
  cat("First different node",k,"\nOLD:\n",paste(deparse(old[[k]]),collapse="\n"),"\nCURRENT:\n",paste(deparse(current[[k]]),collapse="\n"),"\n")
  break
 }
 stop("Non-archive analysis AST differs from the exact historical producer")
}
cat("All original analysis AST nodes preserved after removing only declared archive additions\n")
