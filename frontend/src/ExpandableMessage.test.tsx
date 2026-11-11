import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ExpandableMessage } from "./ExpandableMessage";

afterEach(cleanup);

describe("ExpandableMessage", () => {
  it("keeps the disclosure open when a polled transcript rerenders", () => {
    const view = render(
      <ExpandableMessage preview="CPI · 2026年8月 · 114.3">
        <table><tbody><tr><td>114.3</td></tr></tbody></table>
      </ExpandableMessage>,
    );

    fireEvent.click(screen.getByRole("button", { name: /全文を表示/ }));
    expect(screen.getByRole("button", { name: /全文を閉じる/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("table")).toBeVisible();

    view.rerender(
      <ExpandableMessage preview="CPI · 2026年8月 · 114.3 · updated">
        <table><tbody><tr><td>114.3 updated</td></tr></tbody></table>
      </ExpandableMessage>,
    );

    expect(screen.getByRole("button", { name: /全文を閉じる/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("table")).toBeVisible();
    expect(screen.getByRole("table")).toHaveTextContent("114.3 updated");
  });

  it("lets the reader deliberately collapse the full response", () => {
    render(<ExpandableMessage preview="Latest CPI">表データ</ExpandableMessage>);
    fireEvent.click(screen.getByRole("button", { name: /全文を表示/ }));
    fireEvent.click(screen.getByRole("button", { name: /全文を閉じる/ }));
    expect(screen.getByRole("button", { name: /全文を表示/ })).toHaveAttribute("aria-expanded", "false");
  });

  it("stays open if transcript polling remounts the message subtree", () => {
    const renderMessage = () => (
      <ExpandableMessage stateKey="conversation-1:message-4:assistant" preview="Long response">
        <table><tbody><tr><td>Full result</td></tr></tbody></table>
      </ExpandableMessage>
    );
    const view = render(renderMessage());

    fireEvent.click(screen.getByRole("button", { name: /全文を表示/ }));
    expect(screen.getByRole("table")).toBeVisible();

    view.unmount();
    render(renderMessage());

    expect(screen.getByRole("button", { name: /全文を閉じる/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("table")).toBeVisible();
  });
});
