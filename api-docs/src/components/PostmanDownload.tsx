import { useZudoku } from "zudoku/hooks";
import { Button } from "zudoku/ui/Button.js";
import { Tooltip, TooltipContent, TooltipTrigger } from "zudoku/ui/Tooltip.js";
import { OPENAPI_PATH } from "../openapi";

export function PostmanDownload() {
  const base = useZudoku().options.basePath ?? "";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon-sm" asChild>
          <a href={`${base}${OPENAPI_PATH}`} download aria-label="Download for Postman">
            <img src={`${base}/postman-icon.svg`} alt="" className="size-5" />
          </a>
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom" sideOffset={6}>
        Download for Postman
      </TooltipContent>
    </Tooltip>
  );
}

export default PostmanDownload;
