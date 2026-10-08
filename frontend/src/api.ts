import type {UploadResult} from './types'
export async function api<T>(path:string,options:RequestInit={}):Promise<T>{
  const response=await fetch('/api'+path,{...options,headers:{'Content-Type':'application/json',...options.headers}})
  const data=await response.json().catch(()=>({message:'服务响应格式异常'}))
  if(!response.ok)throw new Error(data.message||`请求失败 (${response.status})`)
  return data as T
}
export const post=<T>(path:string,body:unknown={})=>api<T>(path,{method:'POST',body:JSON.stringify(body)})
export const patch=<T>(path:string,body:unknown)=>api<T>(path,{method:'PATCH',body:JSON.stringify(body)})
export const remove=(path:string)=>api(path,{method:'DELETE'})
export function upload(id:string,files:File[],progress:(n:number)=>void):Promise<UploadResult>{return new Promise((resolve,reject)=>{
  const body=new FormData();files.forEach(file=>{body.append('files',file);body.append('paths',file.webkitRelativePath||file.name)})
  const xhr=new XMLHttpRequest();xhr.open('POST',`/api/data-sources/${id}/upload`)
  xhr.upload.onprogress=e=>{if(e.lengthComputable)progress(Math.round(100*e.loaded/e.total))}
  xhr.onload=()=>{try{const data=JSON.parse(xhr.responseText);if(xhr.status>=400)reject(new Error(data.message));else resolve(data)}catch{reject(new Error('上传响应无法读取'))}}
  xhr.onerror=()=>reject(new Error('上传连接中断'));xhr.send(body)
})}
export async function streamAnswer(path:string,body:unknown,signal:AbortSignal,onEvent:(name:string,data:any)=>void){
  const response=await fetch('/api'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal})
  if(!response.ok){const error=await response.json();throw new Error(error.message||'问答请求失败')}
  if(!response.body)throw new Error('浏览器不支持流式响应')
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',terminal=false
  try{while(true){const {value,done}=await reader.read();buffer+=decoder.decode(value,{stream:!done}).replace(/\r\n/g,'\n');let boundary:number
    while((boundary=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2)
      const name=frame.split('\n').find(x=>x.startsWith('event:'))?.slice(6).trim()
      const data=frame.split('\n').filter(x=>x.startsWith('data:')).map(x=>x.slice(5).trimStart()).join('\n')
      if(name&&data){if(name==='done'||name==='error')terminal=true;onEvent(name,JSON.parse(data))}}
    if(done)break}
    if(!terminal)throw new Error('回答连接中断，输出尚未完成')
  }finally{reader.releaseLock()}
}
