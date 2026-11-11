import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { VirtualCursor, pointerMatchesFrame, type PointerFeedback } from "./VirtualCursor";

const geometry = { displayId: 2, originX: -1200, originY: 100, widthPoints: 600, heightPoints: 400 };
const target = { applicationPid: 42, applicationBundleId: "local.fixture" };
const pointer: PointerFeedback = { phase: "CLICKED", alignment: {
  captureSessionId: "one", targetApplication: target, verifiedAt: 100, verifiedSequence: 8,
  mappedGlobalPoint: [-900, 300], pointerOriginGlobal: [-1200, 100], pointerCurrentGlobal: [-900, 300], distancePoints: Math.hypot(300, 200),
  displayGeometry: geometry, targetBboxNormalized: [.4, .4, .6, .6],
} };
const frame = { captureSessionId: "one", targetApplication: target, sequence: 10, displayGeometry: geometry };

describe("provenance-bound virtual cursor", () => {
  it("maps Retina-sized previews and negative global origins into the same target pixel", () => {
    expect(pointerMatchesFrame(pointer, frame, 101)).toBe(true);
    const { container } = render(<VirtualCursor pointer={pointer} width={1200} height={800} />);
    expect(container.querySelector("circle")).toHaveAttribute("cx", "600");
    expect(container.querySelector("circle")).toHaveAttribute("cy", "400");
    expect(screen.getByRole("status")).toHaveTextContent("移動距離 361 pt");
  });
  it("hides evidence after the target moves, resizes, changes identity or capture session", () => {
    for (const change of [
      { captureSessionId: "two" }, { targetApplication: { ...target, applicationPid: 43 } },
      { targetApplication: { ...target, applicationBundleId: "other" } },
      { displayGeometry: { ...geometry, originX: -1100 } },
      { displayGeometry: { ...geometry, widthPoints: 800 } }, { sequence: 7 },
    ]) expect(pointerMatchesFrame(pointer, { ...frame, ...change }, 101)).toBe(false);
  });
  it("renders the sampled cursor position during bounded pointer movement", () => {
    const sampledPosition: [number, number] = [-1100, 200];
    const aiming = { ...pointer, phase: "AIMING" as const, alignment: {
      ...pointer.alignment, pointerCurrentGlobal: sampledPosition, distancePoints: Math.hypot(200, 100),
    } };
    expect(pointerMatchesFrame(aiming, frame, 101)).toBe(true);
    const { container } = render(<VirtualCursor pointer={aiming} width={1200} height={800} />);
    expect(container.querySelector("svg path:last-of-type")).toHaveAttribute("d", "M 200 200 l 0 20 5 -5 5 9 5 -3 -5 -9 7 -1 Z");
    expect(within(container).getByRole("status")).toHaveTextContent("カーソル移動中");
  });
  it("rejects expired or invalid coordinates rather than drawing a misleading cursor", () => {
    expect(pointerMatchesFrame({ ...pointer, phase: "PLANNED" }, frame, 106)).toBe(false);
    expect(pointerMatchesFrame(pointer, frame, 131)).toBe(false);
    expect(pointerMatchesFrame({ ...pointer, alignment: { ...pointer.alignment, mappedGlobalPoint: [0, 300] } }, frame, 101)).toBe(false);
    expect(pointerMatchesFrame(pointer, frame, 99)).toBe(false);
    expect(pointerMatchesFrame({ ...pointer, alignment: { ...pointer.alignment, mappedGlobalPoint: [NaN, 300] } }, frame, 101)).toBe(false);
    expect(pointerMatchesFrame({ ...pointer, alignment: { ...pointer.alignment, pointerCurrentGlobal: [NaN, 300] } }, frame, 101)).toBe(false);
    expect(pointerMatchesFrame({ ...pointer, alignment: { ...pointer.alignment, targetBboxNormalized: [.6, .4, .4, .6] } }, frame, 101)).toBe(false);
  });
});
