import type {Attachment} from './ChatComposer';
import type {ConceptDiagram} from './conceptDiagramData';

export type ComposerEditTarget =
  | {kind:'message';messageId:string;expectedLastId:string;hasAttachment:boolean}
  | {kind:'instruction';taskId:string;instructionId:string;expectedMessage:string;expectedStatus:'WAITING'|'QUEUED'};
export type ComposerEdit = {
  target:ComposerEditTarget;
  previous:{text:string;attachment:Attachment|null;diagram:ConceptDiagram|null;error:string};
};
